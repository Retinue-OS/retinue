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
  * a committed mark read posts `{seen}` — the row's `inbound_seq` as the
    list shows it, `{}` only where the list states none — to
    /chats/<id>/read, moves the row to Read and offers Undo, which posts
    `{restore, marked}` from the answer and puts the row back; a 409 on Undo
    refetches the list;
  * a mark the server refuses (409: a message landed that the row does not
    show) leaves the row in Unread, offers no Undo and refetches the list;
  * a committed archive posts the flag and moves the row to Archived;
  * a refresh that left before a write is dropped when it answers;
  * the same tray on every tab and channel — Star · (Mark read) · Archive ·
    Mute (Restore · Mute/Unmute on Archived) · Spam (mail only) · Delete;
  * Delete asks for two taps on every tab, for mail and messenger chats
    alike (the armed wording on mail: "Move mails to Trash?"), and a failed
    one comes back with the server's reason and a Retry;
  * Spam (mail only) is deferred: the row leaves, nothing is sent inside the
    15 s window, Undo sends nothing, the window's end sends it, a page hidden
    sends it with keepalive, a failure brings the row back with the reason;
    a Spam the chat page handed over is taken up with the same Undo;
  * multi-select: a long press enters it with the row selected, a tap
    toggles, Shift-click selects a range on a wide screen, the bar offers
    the tab's actions (Spam only when every chat is a mail), each bulk action
    is one request per chat with ONE Undo that restores all, a bulk Delete
    takes two taps, a partial failure is counted and each failed row says
    why, a poll that drops a selected row drops it from the selection, and a
    tab switch, Esc and the back gesture end the mode.

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
      // A notice (no button) is kept as a toast with no Undo.
      set innerHTML(h) { this._h = h; toast = h && !h.includes('<button') ? { text: h, undo: null } : (h ? toast : null); },
      get innerHTML() { return this._h || ''; },
      querySelector(sel) {
        if (sel !== 'button') return { addEventListener() {} };
        return { addEventListener: (_t, f) => { toast = { text: self.shadowRoot._h, undo: f }; } };
      },
    };
    return this.shadowRoot;
  }
}
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
const docListeners = {};
globalThis.document = {
  addEventListener(t, f) { (docListeners[t] ||= []).push(f); },
  removeEventListener(t, f) { docListeners[t] = (docListeners[t] || []).filter((g) => g !== f); },
  createElement: () => new ToastHost(),
  querySelector() { return null; }, hidden: false, visibilityState: 'visible',
  body: { appendChild() {} },
};
globalThis.window = globalThis;
globalThis.location = { hash: '', pathname: '/chats.html', href: 'http://dash/chats.html' };
const winListeners = {};
globalThis.addEventListener = (t, f) => { (winListeners[t] ||= []).push(f); };
globalThis.removeEventListener = (t, f) => { winListeners[t] = (winListeners[t] || []).filter((g) => g !== f); };
const fireWin = (t) => { for (const f of [...(winListeners[t] || [])]) f({ type: t }); };
// The mode's history entry (multi-select), and the back gesture.
globalThis.history = {
  stack: [null],
  get state() { return this.stack[this.stack.length - 1]; },
  pushState(st) { this.stack.push(st); },
  back() { if (this.stack.length > 1) { this.stack.pop(); fireWin('popstate'); } },
};
let wide = false;
globalThis.matchMedia = () => ({ matches: wide, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
const stored = new Map();
globalThis.localStorage = { getItem: (k) => (stored.has(k) ? stored.get(k) : null), setItem: (k, v) => stored.set(k, String(v)), removeItem: (k) => stored.delete(k) };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node' }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };

// ── A scripted gateway ───────────────────────────────────────────────────────
// Requests queue up unanswered; the test answers each one.
const wire = [];
globalThis.fetch = (url, init = {}) => new Promise((resolve) => {
  wire.push({
    url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null,
    keepalive: !!init.keepalive,
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
const { listGroup, deriveListGroup, HANDOFF_KEY } = await import('./chats.js');
const { dismissUndo, flushPendingCommit, hasPendingCommit, UNDO_MS } = await import('./swipe-rows.js');
// What the list tells the shelf (the minimized chat's count).
const { shelf } = await import('./shelf-store.js');
const observed = [];
shelf.observe = (key, obs) => observed.push([key, obs]);

const chat = (id, extra = {}) => ({
  id, channel: 'signal', account: '+41000', key: id, name: 'N' + id, group: false,
  unread: 0, inbound_seq: 5, archived: false, muted: false, starred: false,
  last: { ts: '2026-11-20T10:00:00Z', direction: 'in', kind: 'text', text: 'hi' },
  draft: null, companion: null, messages: `/chats/${id}/messages`, ...extra,
});
// A fresh copy each time: the element writes into the rows it was given.
const payload = (chats, counts) => JSON.parse(JSON.stringify(
  { generated: '2026-11-20T10:00:00Z', ...(counts ? { counts } : {}), chats }));
const rowIds = (html) => [...html.matchAll(/<li class="swipe[^"]*" data-id="([^"]*)"/g)].map((m) => m[1]);
const rowAttrs = (html, id) => {
  const m = new RegExp(`<li class="swipe[^"]*" data-id="${id}"([^>]*)>`).exec(html);
  assert.ok(m, `row ${id} rendered`);
  return { quick: (/data-quick="([^"]*)"/.exec(m[1]) || [])[1] || '',
           left: (/data-quick-left="([^"]*)"/.exec(m[1]) || [])[1] || '' };
};
const liOf = (html, id) => {
  const start = html.search(new RegExp(`<li class="swipe[^"]*" data-id="${id}"`));
  assert.ok(start >= 0, `row ${id} rendered`);
  return html.slice(start, html.indexOf('</li>', start));
};
const trayOf = (html, id) => {
  const start = html.search(new RegExp(`<li class="swipe[^"]*" data-id="${id}"`));
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
  // What ＋ offers is asked once with the first list; nothing, here
  // (tests/test_webapp_mail_writing.py covers ＋).
  take((w) => w.method === 'GET' && w.url === '/chats/compose', 'GET /chats/compose').answer({ channels: [] });
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
    // What a Mark as read from the list hands back, and the mark itself.
    assert.ok(Number.isInteger(c.inbound_seq) && c.inbound_seq >= 0, c.id);
    assert.ok(c.last_read === null || typeof c.last_read === 'string', c.id);
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
  assert.deepEqual(trayOf(html(el), 'u1'), ['star', 'read', 'archive', 'mute', 'delete']);
  tab(el, 'read');
  assert.deepEqual(rowAttrs(html(el), 'r1'), { quick: '', left: 'archive' });
  assert.ok(!html(el).includes('<div class="quick '), 'no right-hand-swipe layer where nothing commits');
  assert.deepEqual(trayOf(html(el), 'r1'), ['star', 'archive', 'mute', 'delete']);
  tab(el, 'archived');
  assert.deepEqual(rowAttrs(html(el), 'a1'), { quick: 'restore', left: '' });
  assert.ok(!html(el).includes('<div class="quick-left'), 'the left swipe opens the tray here');
  assert.deepEqual(trayOf(html(el), 'a1'), ['star', 'restore', 'unmute', 'delete']);
});

await ok('swipe right on Unread marks read: POST {seen}, the row moves to Read, Undo restores', async () => {
  const el = await mount(payload(LIST.map((c) => (c.id === 'u1' ? { ...c, inbound_seq: 12 } : c)), COUNTS));
  el._swipe._onQuick('u1', 'read', null);
  const post = take((w) => w.method === 'POST', 'the read');
  assert.equal(post.url, '/chats/u1/read');
  assert.deepEqual(post.body, { seen: 12 }, 'what the row showed; the client names no instant');
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

await ok('the shelf hears the count the read left, not a zero taken for granted', async () => {
  const el = await mount(payload(LIST, COUNTS));
  observed.length = 0;
  el._markRead('u3');
  take((w) => w.method === 'POST', 'the read')
    .answer({ id: 'u3', last_read: '2026-11-21T09:00:00Z', previous: null, unread: 1, list_group: 'unread' });
  await settle();
  assert.deepEqual(observed.filter(([key]) => key === 'chat:u3').map(([, o]) => o.unread), [1]);
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

await ok('the count a mark names is the newest list\'s; a list that states none posts {}', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el.load();
  takeList().answer(payload(LIST.map((c) => (c.id === 'u3' ? { ...c, inbound_seq: 6 } : c)), COUNTS));
  await settle();
  el._markRead('u3');
  const post = take((w) => w.method === 'POST', 'the read');
  assert.deepEqual(post.body, { seen: 6 });
  post.answer({ id: 'u3', last_read: 'L', previous: null, unread: 0, list_group: 'read' });
  await settle();
  const bare = await mount(payload(LIST.map(({ inbound_seq, ...c }) => c), COUNTS));
  bare._markRead('u1');
  const old = take((w) => w.method === 'POST', 'the read');
  assert.deepEqual(old.body, {}, 'a server older than the field');
  old.answer({ id: 'u1', last_read: 'L', previous: null, unread: 0, list_group: 'read' });
  await settle();
});

await ok('a mark the server refuses (409) leaves the row unread and refetches the list', async () => {
  const el = await mount(payload(LIST, COUNTS));
  toast = null;
  let slidBack = null;
  el._swipe.slide = (li, x) => { slidBack = [li, x]; };
  const row = { id: 'the row element' };
  el._swipe._onQuick('u1', 'read', row);
  take((w) => w.method === 'POST', 'the read')
    .answer({ error: 'a message has arrived since', id: 'u1', last_read: null, unread: 4, list_group: 'unread' }, 409);
  await settle();
  assert.deepEqual(slidBack, [row, 0], 'the swiped row slides back');
  assert.equal(toast, null, 'nothing was marked, so no Undo');
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3', 'u1'], 'still in Unread');
  // The list as it now is: the row shows what came in, and names it next time.
  takeList().answer(payload(LIST.map((c) => (c.id === 'u1'
    ? { ...c, unread: 4, inbound_seq: 6, last: { ts: '2026-11-22T09:00:00Z', direction: 'in', text: 'newer' } } : c)), COUNTS));
  await settle();
  assert.ok(html(el).includes('title="4 unread"'), 'with the message that landed');
  el._markRead('u1');
  assert.deepEqual(take((w) => w.method === 'POST', 'the read').body, { seen: 6 });
});

await ok('a failed read leaves the row in Unread', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._markRead('u1');
  take((w) => w.method === 'POST', 'the read').answer({}, 502);
  await settle();
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3', 'u1']);
  assert.deepEqual(wire.filter((w) => w.method === 'GET'), [], 'an outage is not a reason to refetch');
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

// ── Every action on every tab; Delete in two taps; Spam for mail ─────────────
const mailChat = (id, extra = {}) => chat(id, {
  channel: 'email', account: 'me@example.org', name: 'M' + id, participants: [], ...extra });
const MIXED = [
  chat('su', { unread: 2, list_group: 'unread' }), mailChat('mu', { unread: 1, list_group: 'unread' }),
  chat('sr', { list_group: 'read' }), mailChat('mr', { list_group: 'read' }),
  chat('sa', { list_group: 'archived', archived: true }), mailChat('ma', { list_group: 'archived', archived: true }),
];
const fakeBtn = (confirm) => ({ classList: { add() {}, remove() {} }, dataset: confirm ? { confirm } : {}, textContent: 'Delete' });

await ok('the same tray on every tab and channel; Spam on mail only', async () => {
  const el = await mount(payload(MIXED));
  assert.deepEqual(trayOf(html(el), 'su'), ['star', 'read', 'archive', 'mute', 'delete']);
  assert.deepEqual(trayOf(html(el), 'mu'), ['star', 'read', 'archive', 'mute', 'spam', 'delete']);
  tab(el, 'read');
  assert.deepEqual(trayOf(html(el), 'sr'), ['star', 'archive', 'mute', 'delete']);
  assert.deepEqual(trayOf(html(el), 'mr'), ['star', 'archive', 'mute', 'spam', 'delete']);
  tab(el, 'archived');
  assert.deepEqual(trayOf(html(el), 'sa'), ['star', 'restore', 'mute', 'delete']);
  assert.deepEqual(trayOf(html(el), 'ma'), ['star', 'restore', 'mute', 'spam', 'delete']);
  assert.ok(liOf(html(el), 'ma').includes('title="Moves the thread&#39;s mails to the Junk folder of me@example.org, then mutes and archives the chat"'));
});

await ok('Delete takes two taps on every tab, mail and messenger alike', async () => {
  for (const [scope, sid, mid] of [['unread', 'su', 'mu'], ['read', 'sr', 'mr'], ['archived', 'sa', 'ma']]) {
    const el = await mount(payload(MIXED));
    tab(el, scope);
    const h = html(el);
    const delOf = (id) => (/<button type="button" class="delete"[^>]*>Delete<\/button>/.exec(liOf(h, id)) || [''])[0];
    assert.ok(delOf(mid).includes('data-confirm="Move mails to Trash?"'), delOf(mid));
    assert.ok(delOf(mid).includes('Moves the thread&#39;s mails to the Trash of me@example.org, then erases the chat here'));
    assert.ok(delOf(sid) && !delOf(sid).includes('data-confirm'), delOf(sid));
    for (const id of [sid, mid]) {
      const btn = fakeBtn(id === mid ? 'Move mails to Trash?' : '');
      el._confirmDelete(id, btn);
      assert.equal(btn.textContent, id === mid ? 'Move mails to Trash?' : 'Delete for good?', 'armed, and saying so');
      await settle();
      assert.deepEqual(wire.filter((w) => w.method === 'POST'), [], 'the first tap sends nothing');
      el._confirmDelete(id, btn);
      const post = take((w) => w.method === 'POST', 'the delete');
      assert.equal(post.url, `/chats/${id}/delete`);
      post.answer({ deleted: true });
      await settle();
      assert.ok(!rowIds(html(el)).includes(id), `${scope}: ${id} is gone`);
    }
    assert.equal(toast === null || !toast.text.includes('Deleted'), true, 'no Undo for a delete');
  }
});

await ok('a failed delete comes back with the server\'s reason and a Retry', async () => {
  const el = await mount(payload(MIXED));
  const btn = fakeBtn('Move mails to Trash?');
  el._confirmDelete('mu', btn);
  el._confirmDelete('mu', btn);
  take((w) => w.method === 'POST', 'the delete').answer(
    { error: "the chat's messages could not be completely erased (mail-gateway: the mailbox has no Trash folder)" }, 502);
  await settle();
  const li = liOf(html(el), 'mu');
  assert.ok(li.includes('<li class="swipe failed"') || /class="swipe failed/.test(li), li.slice(0, 80));
  assert.ok(li.includes('Not deleted: The chat&#39;s messages could not be completely erased (mail-gateway: the mailbox has no Trash folder)'), li);
  assert.ok(li.includes('>Retry delete</button>'), li);
  // A deployment without the capability: 503, and no reason given.
  el._confirmDelete('su', fakeBtn(''));
  el._confirmDelete('su', fakeBtn(''));
  take((w) => w.method === 'POST', 'the delete').answer({}, 503);
  await settle();
  assert.ok(liOf(html(el), 'su').includes('Not deleted: Delete not set up'));
  assert.ok(liOf(html(el), 'su').includes('>Delete not set up</button>'));
});

await ok('Spam is deferred: nothing sent in the window, Undo sends nothing, its end sends it', async () => {
  const el = await mount(payload(MIXED));
  assert.equal(UNDO_MS, 15000);
  el._defer('su', 'spam');
  assert.ok(rowIds(html(el)).includes('su'), 'a messenger chat is never spam');
  el._defer('mu', 'spam');
  assert.ok(!rowIds(html(el)).includes('mu'), 'the row leaves at once');
  assert.ok(toast && toast.text.includes('Marked as spam · Mmu'), toast && toast.text);
  // A refresh while it waits does not bring it back.
  el.load();
  takeList().answer(payload(MIXED));
  await settle();
  assert.ok(!rowIds(html(el)).includes('mu'));
  assert.deepEqual(wire.filter((w) => w.method === 'POST'), [], 'nothing sent');
  toast.undo();
  await settle();
  assert.ok(rowIds(html(el)).includes('mu'), 'back');
  assert.equal(hasPendingCommit(), false);
  assert.deepEqual(wire.filter((w) => w.method === 'POST'), [], 'and nothing sent, ever');
  // Again, and the window ends.
  el._defer('mu', 'spam');
  flushPendingCommit(false);
  const post = take((w) => w.method === 'POST', 'the spam');
  assert.equal(post.url, '/chats/mu/spam');
  assert.equal(post.keepalive, false);
  post.answer({ id: 'mu', spam: true, moved: 1, archived: true, muted: true, starred: false });
  await settle();
  assert.ok(!rowIds(html(el)).includes('mu'));
  tab(el, 'archived');
  assert.ok(rowIds(html(el)).includes('mu'), 'in Archived, muted');
});

await ok('a page hidden or left sends the waiting Spam at once, with keepalive', async () => {
  for (const how of ['pagehide', 'hidden']) {
    const el = await mount(payload(MIXED));
    el._defer('mu', 'spam');
    if (how === 'pagehide') fireWin('pagehide');
    else {
      document.visibilityState = 'hidden';
      for (const f of docListeners.visibilitychange || []) f({ type: 'visibilitychange' });
      document.visibilityState = 'visible';
    }
    const post = take((w) => w.method === 'POST', 'the spam');
    assert.equal(post.url, '/chats/mu/spam');
    assert.equal(post.keepalive, true, how);
    post.answer({ archived: true, muted: true });
    await settle();
  }
});

await ok('a failed Spam brings the row back with the reason and a Retry', async () => {
  const el = await mount(payload(MIXED));
  el._defer('mu', 'spam');
  flushPendingCommit(false);
  take((w) => w.method === 'POST', 'the spam').answer({ error: "the mailbox has no Junk folder — set MAIL_JUNK_FOLDER" }, 502);
  await settle();
  const li = liOf(html(el), 'mu');
  assert.ok(li.includes('Not marked as spam: The mailbox has no Junk folder — set MAIL_JUNK_FOLDER'), li);
  assert.ok(li.includes('>Retry spam</button>'), li);
});

await ok('a Spam the chat page handed over is taken up with the same Undo', async () => {
  stored.set(HANDOFF_KEY, JSON.stringify({ id: 'mu', kind: 'spam', name: 'Mmu', at: Date.now() }));
  const el = await mount(payload(MIXED));
  assert.equal(stored.has(HANDOFF_KEY), false, 'taken');
  assert.ok(!rowIds(html(el)).includes('mu'));
  assert.ok(toast && toast.text.includes('Marked as spam · Mmu'));
  assert.deepEqual(wire.filter((w) => w.method === 'POST'), []);
  toast.undo();
  await settle();
  assert.ok(rowIds(html(el)).includes('mu'));
  // A Delete is never handed over (it takes its two taps on the chat page).
  stored.set(HANDOFF_KEY, JSON.stringify({ id: 'su', kind: 'delete', at: Date.now() }));
  const two = await mount(payload(MIXED));
  assert.ok(rowIds(html(two)).includes('su'));
  assert.deepEqual(wire.filter((w) => w.method === 'POST'), []);
});

// ── Multi-select ─────────────────────────────────────────────────────────────
const barOf = (h) => {
  const m = /<div class="select-bar"[\s\S]*?data-bulk="cancel"/.exec(h);
  return m ? m[0] : '';
};
const bulkActs = (h) => [...barOf(h).matchAll(/data-bulk="([a-z]+)"/g)].map((m) => m[1]);
const answerAll = async (pred, doc, status = 200) => {
  await settle();
  const got = wire.filter(pred);
  for (const w of got) { wire.splice(wire.indexOf(w), 1); w.answer(typeof doc === 'function' ? doc(w) : doc, typeof status === 'function' ? status(w) : status); }
  await settle();
  await settle();
  return got;
};

await ok('a long press starts selecting with that row; a tap toggles; the bar counts', async () => {
  const el = await mount(payload(LIST, COUNTS));
  assert.ok(html(el).includes('data-bulk="start"'), 'a Select button');
  el._swipe._onLongPress('u1');
  assert.equal(el._selecting, true);
  assert.equal(history.state && history.state.chatsSelect, true, 'the mode is a history entry');
  let h = html(el);
  assert.ok(barOf(h).includes('1 selected'), barOf(h));
  assert.deepEqual(bulkActs(h), ['read', 'archive', 'mute', 'star', 'delete', 'cancel'],
    'what the tray offers; no Spam for messenger chats');
  assert.ok(/class="swipe selected" data-id="u1"/.test(h));
  assert.ok(liOf(h, 'u1').includes('aria-pressed="true"'));
  el._swipe._onSelect('u3', null, {});
  assert.ok(barOf(html(el)).includes('2 selected'));
  el._swipe._onSelect('u1', null, {});
  assert.deepEqual([...el._selected], ['u3']);
  // Esc ends it, and the history entry with it.
  for (const f of docListeners.keydown || []) f({ key: 'Escape', preventDefault() {} });
  assert.equal(el._selecting, false);
  assert.ok(!history.state || !history.state.chatsSelect);
  assert.ok(!barOf(html(el)));
});

await ok('the back gesture and a tab switch end selection; Cancel too', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._swipe._onLongPress('u1');
  history.back();
  assert.equal(el._selecting, false, 'back');
  el._swipe._onLongPress('u1');
  tab(el, 'read');
  assert.equal(el._selecting, false, 'tab switch');
  assert.equal(el._selected.size, 0);
  tab(el, 'unread');
  el._enterSelect('u2');
  el._bulk('cancel');
  el._exitSelect();
  assert.equal(el._selecting, false);
});

await ok('Shift-click selects a range on a wide screen, not on a phone', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._swipe._onLongPress('u2');
  el._swipe._onSelect('u1', null, { shiftKey: true });
  assert.deepEqual([...el._selected].sort(), ['u1', 'u2'], 'narrow: a toggle');
  el._exitSelect();
  wide = true;
  try {
    el._swipe._onLongPress('u2');       // shown: u2 (starred), u3, u1
    el._swipe._onSelect('u1', null, { shiftKey: true });
    assert.deepEqual([...el._selected].sort(), ['u1', 'u2', 'u3']);
  } finally {
    wide = false;
  }
});

await ok('bulk archive: one request per chat, ONE Undo that restores them all', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._enterSelect('u1');
  el._toggleSelect('u2', {});
  el._toggleSelect('u3', {});
  el._bulk('archive');
  assert.equal(el._selecting, false, 'the mode ends with the action');
  const posts = await answerAll((w) => w.method === 'POST', { archived: true, muted: false, starred: false });
  assert.deepEqual(posts.map((w) => w.url).sort(), ['/chats/u1/flags', '/chats/u2/flags', '/chats/u3/flags']);
  assert.ok(posts.every((w) => JSON.stringify(w.body) === '{"archived":true}'));
  assert.deepEqual(rowIds(html(el)), [], 'all three out of Unread');
  assert.ok(toast && toast.text.includes('Archived 3'), toast && toast.text);
  toast.undo();
  const undos = await answerAll((w) => w.method === 'POST', (w) => ({ archived: false, muted: false, starred: w.url.includes('u2') }));
  assert.deepEqual(undos.map((w) => w.url).sort(), ['/chats/u1/flags', '/chats/u2/flags', '/chats/u3/flags']);
  assert.deepEqual(undos.find((w) => w.url.includes('u2')).body, { archived: false, muted: false, starred: true },
    'each chat\'s previous state, exactly');
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3', 'u1'], 'all back');
});

await ok('bulk mark read, mute, star; restore and unmute on Archived', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._enterSelect('u1');
  el._toggleSelect('u3', {});
  el._bulk('read');
  const reads = await answerAll((w) => w.method === 'POST',
    (w) => ({ id: 'x', last_read: 'L', previous: 'P', unread: 0, list_group: 'read' }));
  assert.deepEqual(reads.map((w) => [w.url, w.body]).sort(), [['/chats/u1/read', { seen: 5 }], ['/chats/u3/read', { seen: 5 }]]);
  assert.deepEqual(rowIds(html(el)), ['u2']);
  assert.ok(toast.text.includes('Marked read 2'));
  toast.undo();
  const back = await answerAll((w) => w.method === 'POST', { last_read: 'P', unread: 2, list_group: 'unread' });
  assert.ok(back.every((w) => JSON.stringify(w.body) === '{"restore":"P","marked":"L"}'));
  assert.deepEqual(rowIds(html(el)).sort(), ['u1', 'u2', 'u3']);
  el._enterSelect('u2');
  assert.ok(bulkActs(html(el)).includes('unstar'), 'every one starred: Unstar');
  el._toggleSelect('u1', {});
  assert.ok(bulkActs(html(el)).includes('star'), 'not every one: Star');
  el._bulk('star');
  const stars = await answerAll((w) => w.method === 'POST', { archived: false, muted: false, starred: true });
  assert.ok(stars.every((w) => JSON.stringify(w.body) === '{"starred":true}') && stars.length === 2);
  el._enterSelect('u1');
  el._bulk('mute');
  const mutes = await answerAll((w) => w.method === 'POST', { archived: true, muted: true, starred: true });
  assert.deepEqual(mutes.map((w) => w.body), [{ muted: true }]);
  tab(el, 'archived');
  el._enterSelect('a1');
  el._toggleSelect('u1', {});
  const acts = bulkActs(html(el));
  assert.deepEqual(acts.slice(0, 2), ['restore', 'unmute'], 'every one muted: Unmute');
  el._bulk('restore');
  const restores = await answerAll((w) => w.method === 'POST', { archived: false, muted: false, starred: false });
  assert.ok(restores.length === 2 && restores.every((w) => JSON.stringify(w.body) === '{"archived":false,"muted":false}'));
});

await ok('a partial failure: counted in the toast, each failed row says why', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._enterSelect('u1');
  el._toggleSelect('u2', {});
  el._toggleSelect('u3', {});
  el._bulk('archive');
  await answerAll((w) => w.method === 'POST',
    (w) => (w.url.includes('u3') ? { error: 'the chat state could not be written' } : { archived: true, muted: false, starred: false }),
    (w) => (w.url.includes('u3') ? 500 : 200));
  assert.ok(toast && toast.text.includes('Archived 2 of 3 — 1 failed'), toast && toast.text);
  assert.deepEqual(rowIds(html(el)), ['u3'], 'the failed one stays');
  assert.ok(liOf(html(el), 'u3').includes('Not archived: The chat state could not be written'));
  toast.undo();
  const undos = await answerAll((w) => w.method === 'POST', { archived: false, muted: false, starred: false });
  assert.equal(undos.length, 2, 'only what was done is undone');
});

await ok('a bulk delete takes two taps, and says what the second does', async () => {
  const el = await mount(payload(MIXED));
  el._enterSelect('su');
  el._toggleSelect('mu', {});
  assert.deepEqual(bulkActs(html(el)), ['read', 'archive', 'mute', 'star', 'delete', 'cancel'], 'a messenger chat among them: no Spam');
  el._bulk('delete');
  await settle();
  assert.deepEqual(wire.filter((w) => w.method === 'POST'), [], 'armed only');
  assert.ok(barOf(html(el)).includes('Delete 2 chats? Mails move to the Trash'), barOf(html(el)));
  el._bulk('delete');
  const dels = await answerAll((w) => w.method === 'POST',
    (w) => (w.url.includes('mu') ? { error: 'the mailbox has no Trash folder' } : { deleted: true }),
    (w) => (w.url.includes('mu') ? 502 : 200));
  assert.deepEqual(dels.map((w) => w.url).sort(), ['/chats/mu/delete', '/chats/su/delete']);
  assert.deepEqual(rowIds(html(el)), ['mu']);
  assert.ok(liOf(html(el), 'mu').includes('Not deleted: The mailbox has no Trash folder'));
  // Messenger chats only: "For good".
  const two = await mount(payload([chat('x1', { unread: 1 }), chat('x2', { unread: 1 })]));
  two._enterSelect('x1');
  two._toggleSelect('x2', {});
  two._bulk('delete');
  assert.ok(barOf(html(two)).includes('Delete 2 chats? For good'));
  two._exitSelect();
});

await ok('a bulk spam: offered only when every chat is a mail; deferred like one', async () => {
  const el = await mount(payload([...MIXED, mailChat('mu2', { unread: 1, list_group: 'unread' })]));
  el._enterSelect('mu');
  el._toggleSelect('mu2', {});
  assert.ok(bulkActs(html(el)).includes('spam'));
  el._bulk('spam');
  assert.deepEqual(rowIds(html(el)), ['su']);
  assert.ok(toast.text.includes('Marked as spam 2'));
  await settle();
  assert.deepEqual(wire.filter((w) => w.method === 'POST'), [], 'nothing sent in the window');
  flushPendingCommit(false);
  const posts = await answerAll((w) => w.method === 'POST', { archived: true, muted: true });
  assert.deepEqual(posts.map((w) => w.url).sort(), ['/chats/mu/spam', '/chats/mu2/spam']);
});

await ok('a deferred bulk spam that partly fails says so, with no Undo', async () => {
  const el = await mount(payload([...MIXED, mailChat('mu2', { unread: 1, list_group: 'unread' }),
    mailChat('mu3', { unread: 1, list_group: 'unread' })]));
  el._enterSelect('mu');
  el._toggleSelect('mu2', {});
  el._toggleSelect('mu3', {});
  el._bulk('spam');
  dismissUndo();      // the window ends: the commit goes out
  await answerAll((w) => w.method === 'POST',
    (w) => (w.url.includes('mu3') ? { error: 'the mailbox has no Junk folder' } : { archived: true, muted: true }),
    (w) => (w.url.includes('mu3') ? 502 : 200));
  await settle();
  assert.ok(toast && toast.text.includes('Marked as spam 2 of 3 — 1 failed'), toast && toast.text);
  assert.equal(toast.undo, null, 'a notice, no Undo');
  assert.deepEqual(rowIds(html(el)).filter((id) => id.startsWith('mu')), ['mu3'], 'the failed one is back');
  assert.ok(liOf(html(el), 'mu3').includes('Not marked as spam: The mailbox has no Junk folder'));
  dismissUndo();
});

await ok('a poll that drops a selected row drops it from the selection', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._enterSelect('u1');
  el._toggleSelect('u3', {});
  el.load();
  takeList().answer(payload(LIST.map((c) => (c.id === 'u3' ? { ...c, unread: 0, list_group: 'read' } : c))));
  await settle();
  assert.deepEqual([...el._selected], ['u1']);
  assert.ok(barOf(html(el)).includes('1 selected'));
  el._exitSelect();
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
