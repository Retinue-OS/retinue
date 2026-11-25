#!/usr/bin/env python3
"""The model trim on the chat page (webapp/components/chat-page.js;
docs/mail-chats.md, "The model trim (phase 4)").

The element is driven under Node with a scripted DOM that has geometry, a
scripted fetch, a fake IntersectionObserver and a fake clock, and pins:

  * first view: a `wanting` mail is asked for once its bubble has been at
    least half visible (half the view, for a bubble taller than it) for
    600 ms without a break — not while it flies past, not while less than
    half of it shows, not while the page is hidden or the companion's pane
    is the one on a phone;
  * what is watched: only `wanting` mails with this chat's own Original —
    never a settled mail, a tombstone, a bubble of the page's own, a mail
    whose Original points elsewhere; nothing at all on a messenger chat;
  * the request: POST to the Original's sibling `/trim`, once per mail per
    page life; an answer `wanting` names when it may be asked again
    (`retry_after`, clamped; a default where it names none), no answer (a
    5xx, offline) a minute;
  * not under one of the page's full-screen layers (the lightbox, the
    attention sheet, a modal), and the dwell starts again when the page
    comes back — hidden and shown, from the back-forward cache, or thawed
    from a freeze in which no timer ran;
  * the answers: 200 settled replaces the text in place, held over a poll
    that read the record before the settle; 202 shows "Tidying up…" (the
    page's own words, never the server's) until a poll shows the record
    settled, or for two minutes at most — put into the bubble's own node
    and taken out of it, never a bubble drawn anew, and nothing at all
    while its Original is open; an older server (405, 501, a 404 that is
    not the endpoint's) switches the feature off quietly for the page's
    life, while the endpoint's own 404 or 410 ends that one mail; 410
    follows a merge;
  * the observer lets go of a bubble that left the DOM or was re-rendered,
    of every bubble on a chat switch and when the element goes, and an
    answer that comes back after a switch touches nothing;
  * the reader's place: a bubble above the view that shrinks leaves the
    message on screen where it was; a reader at the bottom stays there;
  * an open Original survives the text changing under it, and a mail a
    model shortened says so at the top of its Original, alone.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes (the static checks still run).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(HERE, "..", "webapp", "components")
CHAT_PAGE = os.path.join(COMPONENTS, "chat-page.js")

HARNESS = r"""
import assert from 'node:assert/strict';

// ── A fake clock ─────────────────────────────────────────────────────────────
// Every timeout the page sets waits for advance(); the poll's interval is
// cleared by open(). Date.now() is the clock's; `clock += ms` alone is a page
// frozen that long (no timer runs), and a timer overdue then runs at the
// time it is run, not at the time it was due.
const realSetTimeout = globalThis.setTimeout;
let clock = Date.parse('2026-11-24T10:00:00Z');
Date.now = () => clock;
const timers = [];
let timerSeq = 0;
globalThis.setTimeout = (fn, ms = 0, ...rest) => {
  timerSeq += 1;
  timers.push({ id: timerSeq, at: clock + (Number(ms) || 0), fn: () => fn(...rest) });
  return timerSeq;
};
globalThis.clearTimeout = (id) => {
  const i = timers.findIndex((t) => t.id === id);
  if (i >= 0) timers.splice(i, 1);
};
const settle = () => new Promise((r) => realSetTimeout(r, 0));
async function advance(ms) {
  const end = clock + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const t = timers[0];
    if (!t || t.at > end) break;
    timers.shift();
    clock = Math.max(clock, t.at);
    t.fn();
    await settle();
  }
  clock = end;
  await settle();
}

// ── A scripted DOM with geometry ─────────────────────────────────────────────
// The thread is an ordered list of top-level nodes, each the HTML of one
// <div> the page put there; a node's box is where the heights before it put
// it, less the scroll offset (clamped as a browser clamps it). A bubble is as
// tall as its text has lines, plus a line for the trim's status line.
function heightOf(html) {
  const open = html.slice(0, html.indexOf('>') + 1);
  if (open.includes('day-sep') || open.includes('unread-sep')) return 30;
  if (open.includes('data-mark-read-row')) return 40;
  if (!open.includes('data-mid')) return 50;
  if (open.includes(' tomb')) return 40;
  if (open.includes('orig-open')) return html.includes('class="orig"') ? 800 : 200;
  const txt = /<span class="txt">(.*?)<\/span>/s.exec(html);
  const lines = txt ? txt[1].split('<br>').length : 1;
  return 60 + 20 * lines + (html.includes('class="trim-state"') ? 20 : 0);
}
function chunks(html) {
  const out = [];
  let depth = 0;
  let start = -1;
  for (const m of html.matchAll(/<div\b|<\/div>/g)) {
    if (m[0] === '<div') { if (depth === 0) start = m.index; depth += 1; }
    else { depth -= 1; if (depth === 0) out.push(html.slice(start, m.index + 6)); }
  }
  return out;
}
class Ctl {
  constructor(attr, sel = `[${attr}]`) { this.attr = attr; this.sel = sel; this.attrs = new Map(); }
  focus() { document.activeElement = this; }
  hasAttribute(n) { return n === this.attr || this.attrs.has(n); }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  matches(s) { return s === 'button, summary, a[href], [tabindex]' || s === this.sel; }
}
class Slot {
  constructor() { this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
}
class Frame {
  constructor() { this.attrs = new Map(); }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  getAttribute(n) { return this.attrs.has(n) ? this.attrs.get(n) : null; }
}
class Node {
  constructor(thread, html) {
    this.thread = thread;
    this.html = html;
    const open = html.slice(0, html.indexOf('>') + 1);
    const mid = (/data-mid="([^"]*)"/.exec(open) || [])[1];
    this.mid = mid ? mid.replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>') : null;
    this.isEmpty = open.includes('class="center');
    this.height = heightOf(html);
    this._slot = null;
    this._toggle = null;
    this.edits = 0;
  }
  getAttribute(n) { return n === 'data-mid' ? this.mid : null; }
  hasAttribute() { return false; }
  setAttribute() {}
  matches() { return false; }
  focus() { document.activeElement = this; }
  get isConnected() { return !!this.thread && this.thread.nodes.includes(this); }
  getBoundingClientRect() { return this.thread.rectOf(this); }
  // The bubble's own HTML changed in place (an element put in or taken out).
  edit(html) { this.html = html; this.height = heightOf(html); this.edits += 1; }
  querySelector(sel) {
    // The status line, and what it goes in before: the bubble's own children.
    if (sel === '.trim-state') {
      const at = this.html.indexOf(TIDY);
      return at < 0 ? null : { remove: () => this.edit(this.html.slice(0, at) + this.html.slice(at + TIDY.length)) };
    }
    if (sel === '.pend-state, .msg-refused, .orig-bar') {
      const at = Math.min(...['pend-state', 'msg-refused', 'orig-bar']
        .map((c) => this.html.indexOf(`<div class="${c}"`)).filter((i) => i >= 0));
      if (!Number.isFinite(at)) return null;
      return {
        insertAdjacentHTML: (where, h) => {
          assert.equal(where, 'beforebegin');
          this.edit(this.html.slice(0, at) + h + this.html.slice(at));
        },
      };
    }
    if (sel === '[data-orig-frame]') {
      return this.html.includes('data-orig-frame') ? (this._slot = this._slot || new Slot()) : null;
    }
    if (sel === '[data-orig-toggle]') {
      return this.html.includes('data-orig-toggle') ? (this._toggle = this._toggle || new Ctl('data-orig-toggle')) : null;
    }
    return null;
  }
  contains(x) { return !!x && x === this._toggle; }
  remove() { this.thread.nodes = this.thread.nodes.filter((x) => x !== this); }
  replaceWith(n) { const i = this.thread.nodes.indexOf(this); this.thread.nodes[i] = n; n.thread = this.thread; }
  insertAdjacentHTML(where, html) {
    const i = this.thread.nodes.indexOf(this) + (where === 'afterend' ? 1 : 0);
    this.thread.nodes.splice(i, 0, ...chunks(html).map((h) => new Node(this.thread, h)));
  }
}
class Thread {
  constructor(html) {
    this.nodes = chunks(html).map((h) => new Node(this, h));
    this._top = 0;
    this.clientHeight = 300;
  }
  hasAttribute() { return false; }
  setAttribute() {}
  focus() {}
  get scrollHeight() { return Math.max(this.clientHeight, this.nodes.reduce((a, n) => a + n.height, 0)); }
  get scrollTop() { return this._top; }
  set scrollTop(v) { this._top = Math.max(0, Math.min(Number(v) || 0, this.scrollHeight - this.clientHeight)); }
  getBoundingClientRect() { return { top: 0, bottom: this.clientHeight }; }
  rectOf(node) {
    let y = 0;
    for (const n of this.nodes) { if (n === node) break; y += n.height; }
    const top = y - this._top;
    return { top, bottom: top + node.height };
  }
  querySelector(sel) {
    if (sel === ':scope > .center') return this.nodes.find((n) => n.isEmpty) || null;
    return null;
  }
  querySelectorAll(sel) {
    if (sel === '[data-mid]') return this.nodes.filter((n) => n.mid);
    if (sel === '.day-sep, .unread-sep') return [];
    return [];
  }
  insertAdjacentHTML(where, html) { this.nodes.push(...chunks(html).map((h) => new Node(this, h))); }
  node(mid) { return this.nodes.find((n) => n.mid === mid); }
}
const registry = new Map();
class FakeElement {
  constructor() { this._attrs = new Map(); this.shadowRoot = null; this.style = {}; this.dataset = {}; }
  attachShadow() { this.shadowRoot = { innerHTML: '', querySelector() { return null; }, querySelectorAll() { return []; } }; return this.shadowRoot; }
  addEventListener() {}
  removeEventListener() {}
  dispatchEvent() { return true; }
  getAttribute(n) { return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v) { this._attrs.set(n, String(v)); }
  hasAttribute(n) { return this._attrs.has(n); }
  removeAttribute(n) { this._attrs.delete(n); }
  toggleAttribute(n, on) { if (on) this._attrs.set(n, ''); else this._attrs.delete(n); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  get isConnected() { return true; }
}
// What the page listens for on the document and the window, so a test can say
// the page was hidden, shown, or shown again from the back-forward cache.
const heard = { document: [], window: [] };
const listen = (where) => ({
  addEventListener(type, fn) { heard[where].push({ type, fn }); },
  removeEventListener(type, fn) { heard[where] = heard[where].filter((l) => !(l.type === type && l.fn === fn)); },
});
const emit = (where, type, ev = {}) => {
  for (const l of [...heard[where]]) if (l.type === type) l.fn({ type, ...ev });
};
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
const layers = new Set();
globalThis.document = {
  ...listen('document'),
  createElement: (tag) => {
    if (tag === 'iframe') return new Frame();
    if (tag === 'template') {
      return {
        set innerHTML(h) {
          const c = chunks(h);
          this.content = { firstElementChild: c.length ? new Node(null, c[0]) : null };
        },
      };
    }
    return new FakeElement();
  },
  // A full-screen layer of the document's (the attention sheet) is up
  // while `layers` holds its selector.
  querySelector(sel) {
    return [...layers].some((l) => String(sel).split(',').map((x) => x.trim()).includes(l)) ? {} : null;
  },
  getElementById() { return null; },
  hidden: false, visibilityState: 'visible', body: { appendChild() {} },
  documentElement: { dataset: {} }, title: 'Retinue — Chat', activeElement: null,
};
globalThis.window = globalThis;
globalThis.innerHeight = 800;
const replaced = [];
globalThis.location = { hash: '', pathname: '/chat.html', search: '', href: 'http://dash/chat.html',
                        origin: 'http://dash', replace: (u) => replaced.push(String(u)) };
globalThis.addEventListener = listen('window').addEventListener;
globalThis.removeEventListener = listen('window').removeEventListener;
globalThis.dispatchEvent = () => true;
globalThis.history = { length: 1, pushState() {}, replaceState() {}, back() {} };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
const store = new Map();
globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
globalThis.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', mediaDevices: null }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };

// ── A fake IntersectionObserver ──────────────────────────────────────────────
// It records what is observed; a test says when a node comes on screen or
// goes (fire), as the browser would.
const observers = [];
globalThis.IntersectionObserver = class {
  constructor(cb, opts) { this.cb = cb; this.opts = opts; this.nodes = new Set(); this.live = true; observers.push(this); }
  observe(n) { assert.ok(this.live, 'observe on a disconnected observer'); this.nodes.add(n); }
  unobserve(n) { this.nodes.delete(n); }
  disconnect() { this.nodes.clear(); this.live = false; }
};
const watched = () => observers.filter((o) => o.live).flatMap((o) => [...o.nodes]);
const watchedMids = () => watched().map((n) => n.mid).sort();
function fire(el, mid, on = true) {
  const node = el._thread.node(mid);
  const o = observers.find((x) => x.live && x.nodes.has(node));
  assert.ok(o, `${mid} is observed`);
  o.cb([{ target: node, isIntersecting: on, intersectionRatio: on ? 1 : 0 }]);
}

// ── A scripted gateway ───────────────────────────────────────────────────────
const wire = [];
const log = [];
globalThis.fetch = (url, init = {}) => {
  const req = { url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null };
  log.push(req);
  return new Promise((resolve, reject) => {
    req.answer = (doc, status = 200) => resolve({ ok: status < 400, status, json: async () => doc });
    // An answer with no JSON body (a proxy's page, an empty 404).
    req.answerRaw = (status) => resolve({ ok: status < 400, status, json: async () => { throw new SyntaxError('no JSON'); } });
    req.fail = () => reject(new TypeError('offline'));
    wire.push(req);
  });
};
const take = (pred, what) => {
  const i = wire.findIndex(pred);
  assert.ok(i >= 0, `expected ${what}, saw ${JSON.stringify(wire.map((w) => [w.method, w.url]))}`);
  return wire.splice(i, 1)[0];
};
const trims = () => log.filter((r) => r.method === 'POST' && r.url.endsWith('/trim'));

await import('./chat-page.js');
const Page = registry.get('retinue-chat-page');
const { shelf } = await import('./shelf-store.js');
shelf.observe = () => {};

let passed = 0;
async function ok(name, fn) { await fn(); passed += 1; console.log('  ok  ' + name); }

// ── The chat ─────────────────────────────────────────────────────────────────
const ACCT = 'me@example.org';
const ID = 'email:~me@example.org:root@mail.example.com';
const ENC = encodeURIComponent(ID);
const MSG_URL = `/chats/${ENC}/messages`;
const orig = (tok, enc = ENC) => `/chats/${enc}/messages/${tok}/original`;
const trimOf = (tok, enc = ENC) => `/chats/${enc}/messages/${tok}/trim`;
const summary = (extra = {}) => ({
  id: ID, channel: 'email', account: ACCT, key: 'root@mail.example.com', name: 'Quarterly numbers', group: false,
  participants: [{ address: 'ann@example.com', name: 'Ann Example', contact: null }], participants_more: 0,
  peer: { address: 'ann@example.com', name: 'Ann Example' },
  unread: 0, list_group: 'read', last_read: null, inbound_seq: 3, archived: false, muted: false, starred: false,
  companion: null, instructed: null, draft: null, messages: MSG_URL,
  can_send: false, cannot_send_reason: 'Read-only here.', ...extra,
});
const lines = (n, word = 'line') => Array.from({ length: n }, (_, i) => `${word} ${i + 1}`).join('\n');
const mail = (id, extra = {}) => ({
  id, chat: ID, direction: 'in', ts: '2026-11-20T08:00:00Z', text: `text of ${id}`, sender: 'ann@example.com',
  sender_name: 'Ann Example', subject: 'Quarterly numbers', recipients: [ACCT], cc: [], trim: 'deterministic',
  removed: false, removed_at: null, original: orig(`t${id}`), attachments: [], ...extra,
});
// Laid out from the top (scrollTop 0): DAY 0–30, w1 30–150, d1 150–230,
// w2 230–310, t1 310–350, mdl 350–430, local-x 430–510, w3 510–590,
// w4 590–1250 (taller than the 300px view).
const BASE = [
  mail('w1', { trim: 'wanting', text: lines(3) }),
  mail('d1'),
  mail('w2', { trim: 'wanting' }),
  mail('t1', { trim: 'wanting', removed: true, removed_at: '2026-11-21T08:00:00Z', text: '', original: null }),
  mail('mdl', { trim: 'model' }),
  mail('local-x', { trim: 'wanting', direction: 'out' }),
  mail('w3', { trim: 'wanting', original: orig('tw3', encodeURIComponent('email:~me@example.org:other@x')) }),
  mail('w4', { trim: 'wanting', text: lines(30) }),
];
let renders = 0;
function fakeRender(el) {
  // The page's full rebuild as far as the thread goes: the thread from
  // _chatThreadHtml, the open Originals' frames, the trim's watch — the
  // steps of render() that concern the mirror (render's own source is
  // checked to make the last call, below).
  el.render = function () {
    renders += 1;
    const thread = this._state === 'ok' ? new Thread(this._chatThreadHtml()) : null;
    this.shadowRoot = { querySelector: (s) => (s === '[data-chat-thread]' ? thread : null), querySelectorAll: () => [] };
    this._thread = thread;
    if (thread) this._mountFrames();
    this._trimSweep();
  };
}
async function open(chat = {}, messages = BASE) {
  wire.length = 0;
  log.length = 0;
  timers.length = 0;
  observers.length = 0;
  replaced.length = 0;
  heard.document = [];
  heard.window = [];
  layers.clear();
  renders = 0;
  document.hidden = false;
  document.activeElement = null;
  const el = new Page();
  el.attachShadow({ mode: 'open' });
  el._id = chat.id || ID;
  fakeRender(el);
  const loading = el._load();
  take((w) => w.url === '/chats', 'the list').answer({ chats: [summary(chat)] });
  await settle();
  take((w) => w.url === (chat.messages || MSG_URL), 'the messages')
    .answer({ chat: summary(chat), messages: JSON.parse(JSON.stringify(messages)) });
  await loading;
  clearInterval(el._pollTimer);
  return el;
}
async function poll(el, messages, chat = {}) {
  const p = el._poll();
  take((w) => w.url === el._msgUrl && w.method === 'GET', 'the poll')
    .answer({ chat: summary(chat), messages: JSON.parse(JSON.stringify(messages)) });
  await p;
}
const with_ = (id, extra, list = BASE) => list.map((m) => (m.id === id ? { ...m, ...extra } : m));
const bubble = (el, mid) => { const n = el._thread.node(mid); assert.ok(n, `bubble ${mid}`); return n.html; };
// w1 seen for the dwell: the request leaves.
async function ask(el, mid = 'w1', url = trimOf(`t${mid}`)) {
  fire(el, mid);
  await advance(600);
  return take((w) => w.method === 'POST' && w.url === url, `the trim request of ${mid}`);
}
const TIDY = '<div class="trim-state" role="status">Tidying up&#8230;</div>';
// A mail's Original as the endpoint answers it.
const DOC = (tok) => ({
  id: 'x', headers: { from: 'Ann <ann@example.com>', subject: 'Quarterly numbers' }, header_block: 'From: Ann',
  text: 'Full text', html_url: `${orig(tok)}.html`, attachments: [],
});

// ── What is watched ──────────────────────────────────────────────────────────
await ok('only wanting mails with this chat\'s own Original are watched', async () => {
  const el = await open();
  assert.deepEqual(watchedMids(), ['w1', 'w2', 'w4'],
    'not a settled mail, a tombstone, a bubble of the page\'s own, or a foreign Original');
  assert.equal(observers.length, 1, 'one observer for the page');
  assert.equal(observers[0].opts.root, undefined, 'the viewport is the root: a pane scrolled aside is not on screen');
  assert.equal(trims().length, 0, 'nothing is asked for on load');
});

await ok('a messenger chat: no observer, no request', async () => {
  const SIG = 'signal:~+41000:+41999';
  const enc = encodeURIComponent(SIG);
  const msgs = [mail('s1', { chat: SIG, trim: 'wanting', original: orig('ts1', enc) })];
  const el = await open({ id: SIG, channel: 'signal', key: '+41999', messages: `/chats/${enc}/messages`,
                          participants: undefined, peer: undefined }, msgs);
  assert.equal(observers.length, 0, 'no observer at all');
  el._trimTick('s1');
  await el._askTrim('s1');
  await advance(5000);
  assert.equal(trims().length, 0);
});

// ── First view ───────────────────────────────────────────────────────────────
await ok('seen for 600 ms: one POST to the Original\'s sibling, and never again', async () => {
  const el = await open();
  fire(el, 'w1');
  await advance(400);
  assert.equal(trims().length, 0, 'not before the dwell');
  await advance(200);
  const req = take((w) => w.method === 'POST', 'the trim request');
  assert.equal(req.url, trimOf('tw1'), 'the token is the Original\'s, the URL its sibling');
  assert.deepEqual(req.body, {});
  assert.ok(!watchedMids().includes('w1'), 'asked: watched no more');
  req.answer({ state: 'queued' }, 202);
  await settle();
  // A full render, a poll, the bubble on screen again: nothing asks twice.
  el.render();
  assert.ok(!watchedMids().includes('w1'));
  await poll(el, BASE);
  await advance(10 * 60 * 1000);
  assert.equal(trims().length, 1, 'one request per mail per page life');
});

await ok('a bubble that only flies past is not asked for; its time starts again', async () => {
  const el = await open();
  fire(el, 'w2');
  await advance(300);
  fire(el, 'w2', false);
  await advance(2000);
  assert.equal(trims().length, 0);
  fire(el, 'w2');
  await advance(599);
  assert.equal(trims().length, 0, 'the dwell counts from its return');
  await advance(1);
  assert.equal(trims().length, 1);
  assert.equal(trims()[0].url, trimOf('tw2'));
});

await ok('a second word from the observer while it stays on screen does not restart the dwell', async () => {
  const el = await open();
  fire(el, 'w1');
  await advance(400);
  fire(el, 'w1');
  await advance(200);
  assert.equal(trims().length, 1);
});

await ok('less than half of it in view is not seen; half is', async () => {
  const el = await open();
  el._thread.scrollTop = 280;              // w2 at -50..30: 30 of 80 shown
  fire(el, 'w2');
  await advance(3000);
  assert.equal(trims().length, 0);
  el._thread.scrollTop = 260;              // -30..50: 50 of 80
  await advance(800);
  assert.equal(trims().length, 1);
});

await ok('a bubble taller than the view is seen when it takes half the view', async () => {
  const el = await open();
  el._thread.scrollTop = 340;              // w4 at 250..910: 50 of the 300 view
  fire(el, 'w4');
  await advance(3000);
  assert.equal(trims().length, 0);
  el._thread.scrollTop = 700;              // w4 fills the view
  await advance(800);
  assert.equal(trims().length, 1);
  assert.equal(trims()[0].url, trimOf('tw4'));
});

await ok('no request while the page is hidden; the dwell counts from its return', async () => {
  const el = await open();
  document.hidden = true;
  fire(el, 'w1');
  await advance(10000);
  assert.equal(trims().length, 0);
  document.hidden = false;
  await advance(500);
  assert.equal(trims().length, 0, 'not the moment it shows again');
  await advance(400);
  assert.equal(trims().length, 1);
});

await ok('on a phone, the companion\'s pane shown: the chat is not seen', async () => {
  const el = await open();
  el._pane = 'companion';
  fire(el, 'w1');
  await advance(5000);
  assert.equal(trims().length, 0);
  el._pane = 'chat';
  await advance(900);
  assert.equal(trims().length, 1);
});

await ok('under the lightbox, the attention sheet or a modal of the page\'s own: not seen', async () => {
  for (const how of ['lightbox', 'sheet', 'modal']) {
    const el = await open();
    const query = el.shadowRoot.querySelector;
    const cover = (on) => {
      if (how === 'lightbox') el._lightbox = on ? { url: '/img', alt: 'Image' } : null;
      else if (how === 'sheet') { if (on) layers.add('retinue-attention-sheet:not([hidden])'); else layers.clear(); }
      else el.shadowRoot.querySelector = on ? (q) => (q === '[aria-modal="true"]' ? {} : query(q)) : query;
    };
    cover(true);
    fire(el, 'w1');
    await advance(5000);
    assert.equal(trims().length, 0, `${how}: a bubble under it is not seen`);
    cover(false);
    await advance(500);
    assert.equal(trims().length, 0, `${how}: the dwell counts from when it went`);
    await advance(400);
    assert.equal(trims().length, 1, how);
  }
});

await ok('a page frozen in the background: the dwell starts again on its return', async () => {
  const el = await open();
  fire(el, 'w1');
  await advance(400);
  clock += 60000;                          // frozen: not one timer ran
  await advance(1);
  assert.equal(trims().length, 0, 'the overdue look is no look at all');
  await advance(500);
  assert.equal(trims().length, 0);
  await advance(100);
  assert.equal(trims().length, 1, 'seen for the dwell after the return');
});

await ok('hidden and shown again, or shown from the back-forward cache: the dwell starts again', async () => {
  for (const how of ['visibilitychange', 'pageshow']) {
    const el = await open();
    fire(el, 'w1');
    await advance(400);
    if (how === 'pageshow') {
      emit('window', 'pageshow', { persisted: true });
    } else {
      document.hidden = true;
      emit('document', 'visibilitychange');
      document.hidden = false;
      emit('document', 'visibilitychange');
    }
    await advance(400);
    assert.equal(trims().length, 0, `${how}: the time before it does not count`);
    await advance(200);
    assert.equal(trims().length, 1, how);
  }
  // A fresh load's pageshow is no return; and the listeners go with the watch.
  const el = await open();
  fire(el, 'w1');
  await advance(400);
  emit('window', 'pageshow', { persisted: false });
  await advance(200);
  assert.equal(trims().length, 1);
  el._trimRelease();
  assert.equal(heard.window.filter((l) => l.type === 'pageshow').length, 0, 'no pageshow listener left');
  assert.equal(el._trimOnShow, null);
});

// ── The answers ──────────────────────────────────────────────────────────────
await ok('200 settled: the text in place, escaped, held over a poll that read the record first', async () => {
  const el = await open();
  const req = await ask(el);
  const before = el._thread.node('d1');
  req.answer({ state: 'model', text: 'Short <img src=x onerror=alert(1)> reply' });
  await settle();
  assert.ok(bubble(el, 'w1').includes('Short &lt;img src=x onerror=alert(1)&gt; reply'), bubble(el, 'w1'));
  assert.ok(!bubble(el, 'w1').includes('<img'));
  assert.ok(!bubble(el, 'w1').includes('trim-state'));
  assert.equal(el._messages.find((m) => m.id === 'w1').trim, 'model');
  assert.equal(el._thread.node('d1'), before, 'the other bubbles untouched');
  // A poll whose document predates the settle: still wanting, the old text.
  const node = el._thread.node('w1');
  await poll(el, BASE);
  assert.equal(el._thread.node('w1'), node, 'the settled text stands');
  assert.ok(bubble(el, 'w1').includes('Short &lt;img'));
  // The record as it now is: nothing to draw again.
  const settled = with_('w1', { trim: 'model', text: 'Short <img src=x onerror=alert(1)> reply' });
  await poll(el, settled);
  assert.equal(el._thread.node('w1'), node);
  // And a later change of the record is the server's word again.
  await poll(el, with_('w1', { trim: 'model', text: 'Newer' }));
  assert.ok(bubble(el, 'w1').includes('Newer'));
  assert.equal(trims().length, 1);
});

await ok('200 deterministic settles too; a poll that settled it first wins', async () => {
  let el = await open();
  let req = await ask(el);
  req.answer({ state: 'deterministic', text: 'text of w1 kept' });
  await settle();
  assert.equal(el._messages.find((m) => m.id === 'w1').trim, 'deterministic');
  el = await open();
  req = await ask(el);
  await poll(el, with_('w1', { trim: 'model', text: 'From the poll' }));
  req.answer({ state: 'model', text: 'From the answer' });
  await settle();
  assert.ok(bubble(el, 'w1').includes('From the poll'), 'the answer does not overwrite a record already settled');
});

await ok('202: "Tidying up…" in the page\'s own words until a poll shows it settled', async () => {
  const el = await open();
  const req = await ask(el);
  req.answer({ state: 'running', text: '<b>injected</b>', message: '<img src=x>' }, 202);
  await settle();
  const html = bubble(el, 'w1');
  assert.ok(html.includes(TIDY), html);
  assert.ok(html.indexOf(TIDY) > html.indexOf('class="txt"'), 'under the text');
  assert.ok(!html.includes('injected') && !html.includes('<img'), 'nothing of the answer is shown');
  await poll(el, BASE);
  assert.ok(bubble(el, 'w1').includes(TIDY), 'an unchanged record leaves it');
  await poll(el, with_('w1', { trim: 'model', text: 'Tidy' }));
  assert.ok(!bubble(el, 'w1').includes('trim-state'));
  assert.ok(bubble(el, 'w1').includes('Tidy'));
  assert.equal(trims().length, 1);
});

await ok('202: the status line goes after two minutes without a settle, and no second request', async () => {
  const el = await open();
  const req = await ask(el);
  req.answer({ state: 'queued' }, 202);
  await settle();
  assert.ok(bubble(el, 'w1').includes(TIDY));
  await advance(119000);
  assert.ok(bubble(el, 'w1').includes(TIDY));
  await advance(1000);
  assert.ok(!bubble(el, 'w1').includes('trim-state'));
  assert.ok(!watchedMids().includes('w1'));
  await poll(el, BASE);
  await advance(10 * 60 * 1000);
  assert.equal(trims().length, 1);
});

await ok('202: the status line goes into the bubble\'s own node, and out of it; the focus stays', async () => {
  const el = await open();
  const req = await ask(el);
  const node = el._thread.node('w1');
  const toggle = node.querySelector('[data-orig-toggle]');
  toggle.focus();
  req.answer({ state: 'queued' }, 202);
  await settle();
  assert.equal(el._thread.node('w1'), node, 'not drawn anew');
  assert.ok(node.html.includes(TIDY));
  assert.ok(node.html.indexOf(TIDY) < node.html.indexOf('class="orig-bar"'), 'above the Original control');
  assert.equal(node.edits, 1);
  assert.equal(document.activeElement, toggle);
  await advance(120000);
  assert.equal(el._thread.node('w1'), node);
  assert.ok(!node.html.includes('trim-state'));
  assert.equal(node.edits, 2);
  assert.equal(document.activeElement, toggle);
  // The chat gone: a line it showed is taken out the same way.
  const el2 = await open();
  const r2 = await ask(el2);
  r2.answer({ state: 'running' }, 202);
  await settle();
  const n2 = el2._thread.node('w1');
  el2._stopPolling();
  assert.equal(el2._thread.node('w1'), n2);
  assert.ok(!n2.html.includes('trim-state'));
});

await ok('202 while the Original is open: nothing at all is done to the bubble or its frame', async () => {
  for (const close of ['before the cap', 'after the cap']) {
    const el = await open();
    el._toggleOriginal('w1');
    take((w) => w.method === 'GET' && w.url === orig('tw1'), 'the original').answer(DOC('tw1'));
    await settle();
    await settle();
    const node = el._thread.node('w1');
    assert.ok(node.html.includes('orig-open'));
    const frame = node._slot.children[0];
    const html = node.html;
    const req = await ask(el);
    req.answer({ state: 'queued' }, 202);
    await settle();
    assert.equal(el._thread.node('w1'), node, `${close}: not drawn anew`);
    assert.equal(node.html, html, close);
    assert.equal(node.edits, 0, close);
    assert.equal(node._slot.children.length, 1, `${close}: no second frame`);
    assert.equal(node._slot.children[0], frame, `${close}: the frame is the one the user scrolled`);
    if (close === 'after the cap') {
      await advance(120000);
      assert.equal(el._thread.node('w1'), node, 'the cap does nothing either');
      assert.equal(node.edits, 0);
      el._toggleOriginal('w1');
      assert.ok(!bubble(el, 'w1').includes('trim-state'), 'closed after the cap: no line');
    } else {
      await advance(1000);
      el._toggleOriginal('w1');
      assert.ok(bubble(el, 'w1').includes(TIDY), 'closed while the turn is on its way: the line');
      const closed = el._thread.node('w1');
      await advance(120000);
      assert.equal(el._thread.node('w1'), closed);
      assert.ok(!closed.html.includes('trim-state'), 'and it goes at the cap, in place');
    }
  }
});

await ok('202 after a poll already brought the settled record: no status line', async () => {
  const el = await open();
  const req = await ask(el);
  await poll(el, with_('w1', { trim: 'model', text: 'Settled first' }));
  req.answer({ state: 'running' }, 202);
  await settle();
  assert.ok(!bubble(el, 'w1').includes('trim-state'));
  assert.ok(bubble(el, 'w1').includes('Settled first'));
});

await ok('200 wanting: asked again only once retry_after has passed, and seen again', async () => {
  const el = await open();
  let req = await ask(el);
  req.answer({ state: 'wanting', retry_after: 120 });
  await settle();
  assert.ok(watchedMids().includes('w1'), 'watched again, for later');
  assert.ok(!bubble(el, 'w1').includes('trim-state'), 'no status line: nothing is on its way');
  fire(el, 'w1');
  await advance(119000);
  assert.equal(trims().length, 1, 'not before retry_after');
  await advance(1000 + 800);
  req = take((w) => w.method === 'POST' && w.url === trimOf('tw1'), 'the second request');
  assert.equal(trims().length, 2);
  req.answer({ state: 'queued' }, 202);
  await settle();
});

await ok('retry_after is held to bounds; none named waits ten minutes', async () => {
  let el = await open();
  let req = await ask(el);
  req.answer({ state: 'wanting', retry_after: 0 });
  await settle();
  fire(el, 'w1');
  await advance(29000);
  assert.equal(trims().length, 1, 'never sooner than 30 s');
  await advance(1000 + 800);
  assert.equal(trims().length, 2);
  el = await open();
  req = await ask(el);
  req.answer({ state: 'wanting' });
  await settle();
  fire(el, 'w1');
  await advance(599000);
  assert.equal(trims().length, 1);
  await advance(1000 + 800);
  assert.equal(trims().length, 2);
  el = await open();
  req = await ask(el);
  req.answer({ state: 'wanting', retry_after: 'soon' });
  await settle();
  fire(el, 'w1');
  await advance(599000);
  assert.equal(trims().length, 1, 'a retry_after that is no number is none');
});

await ok('no answer (5xx, offline): asked again after a minute', async () => {
  for (const how of ['503', 'offline']) {
    const el = await open();
    const req = await ask(el);
    if (how === 'offline') req.fail(); else req.answer({ error: 'x' }, 503);
    await settle();
    fire(el, 'w1');
    await advance(59000);
    assert.equal(trims().length, 1, how);
    await advance(1000 + 800);
    assert.equal(trims().length, 2, how);
  }
});

await ok('an older server (no such endpoint): off for the page\'s life, quietly', async () => {
  for (const [status, body] of [[404, { error: 'not found' }], [404, null], [404, {}], [405, { error: 'not found' }],
                                [501, null]]) {
    const el = await open();
    const req = await ask(el);
    if (body) req.answer(body, status); else req.answerRaw(status);
    await settle();
    assert.equal(watched().length, 0, `${status}: nothing is watched`);
    assert.ok(observers.every((o) => !o.live), `${status}: the observer is let go`);
    assert.ok(!bubble(el, 'w1').includes('trim-state'));
    assert.ok(bubble(el, 'w1').includes('line 1'), 'the deterministic text stays');
    // A full render and a newly arrived wanting mail: still nothing.
    el.render();
    await poll(el, [...BASE, mail('w9', { trim: 'wanting', ts: '2026-11-20T09:00:00Z' })]);
    assert.equal(watched().length, 0);
    await advance(10 * 60 * 1000);
    assert.equal(trims().length, 1);
  }
});

await ok('the endpoint\'s own 404 or 410 ends that one mail; the others stay watched', async () => {
  for (const [status, error] of [[404, 'no such mail in this chat'], [410, 'this mail is gone from the mailbox']]) {
    const el = await open();
    const req = await ask(el);
    req.answer({ error }, status);
    await settle();
    await settle();
    assert.equal(el._trimOff, false, `${status}: the feature stays on`);
    assert.deepEqual(replaced, [], `${status}: the page stays`);
    assert.equal(el._pollDone, false, `${status}: the chat is not over`);
    assert.deepEqual(watchedMids(), ['w2', 'w4'], `${status}: the others are watched`);
    const r2 = await ask(el, 'w2');
    r2.answer({ state: 'queued' }, 202);
    await settle();
    assert.ok(bubble(el, 'w2').includes(TIDY), status);
    await advance(10 * 60 * 1000);
    assert.deepEqual(trims().map((r) => r.url), [trimOf('tw1'), trimOf('tw2')], `${status}: w1 not asked again`);
  }
});

await ok('410: a merge is followed; a mail gone is not asked again', async () => {
  let el = await open();
  let req = await ask(el);
  req.answer({ merged_into: 'email:~me@example.org:survivor@x' }, 410);
  await settle();
  await settle();
  assert.deepEqual(replaced, [`/chat.html?id=${encodeURIComponent('email:~me@example.org:survivor@x')}`]);
  el = await open();
  req = await ask(el);
  req.answer({ error: 'gone' }, 410);
  await settle();
  await settle();
  assert.deepEqual(replaced, []);
  assert.ok(!watchedMids().includes('w1'));
  await advance(10 * 60 * 1000);
  assert.equal(trims().length, 1);
});

await ok('an answer of no known shape is the end of it', async () => {
  const el = await open();
  const req = await ask(el);
  const node = el._thread.node('w1');
  req.answer({ state: '<script>x</script>', text: 'nope' });
  await settle();
  assert.equal(el._thread.node('w1'), node, 'nothing drawn');
  assert.ok(!watchedMids().includes('w1'));
  await advance(10 * 60 * 1000);
  assert.equal(trims().length, 1);
});

// ── The watch follows the thread ─────────────────────────────────────────────
await ok('a bubble that leaves the DOM or is drawn anew: the old node is let go', async () => {
  const el = await open();
  const w2 = el._thread.node('w2');
  await poll(el, with_('w2', { removed: true, removed_at: '2026-11-22T08:00:00Z', text: '', original: null }));
  assert.ok(!watched().includes(w2), 'a tombstone is watched no more');
  assert.deepEqual(watchedMids(), ['w1', 'w4']);
  const old = watched();
  el.render();
  assert.ok(old.every((n) => !watched().includes(n)), 'no node of the old thread is watched');
  assert.deepEqual(watchedMids(), ['w1', 'w4']);
  assert.ok(watched().every((n) => n.isConnected), 'only nodes in the DOM');
  // Settled by the poll before it was seen: let go, never asked.
  await poll(el, with_('w1', { trim: 'model', text: 'done elsewhere' },
                       with_('w2', { removed: true, removed_at: '2026-11-22T08:00:00Z', text: '', original: null })));
  assert.deepEqual(watchedMids(), ['w4']);
  // A wanting mail that arrives is watched.
  await poll(el, [...BASE, mail('w9', { trim: 'wanting', ts: '2026-11-20T09:00:00Z' })]);
  assert.ok(watchedMids().includes('w9'));
  assert.equal(trims().length, 0);
});

await ok('a chat switch lets everything go, and a late answer touches nothing', async () => {
  const el = await open();
  const req = await ask(el);
  const first = observers.slice();
  // The element loads another chat; a mail there shares the id.
  const ID2 = 'email:~me@example.org:second@mail.example.com';
  const enc2 = encodeURIComponent(ID2);
  const msgs2 = [mail('w1', { chat: ID2, trim: 'wanting', text: 'chat two', original: orig('tw1', enc2) })];
  el._id = ID2;
  const loading = el._load();
  take((w) => w.url === '/chats', 'the list').answer({ chats: [summary({ id: ID2, messages: `/chats/${enc2}/messages` })] });
  await settle();
  take((w) => w.url === `/chats/${enc2}/messages`, 'the messages')
    .answer({ chat: summary({ id: ID2, messages: `/chats/${enc2}/messages` }), messages: msgs2 });
  await loading;
  clearInterval(el._pollTimer);
  assert.ok(first.every((o) => !o.live), 'the first chat\'s observer is let go');
  const node = el._thread.node('w1');
  req.answer({ state: 'model', text: 'from chat one' });
  await settle();
  assert.equal(el._thread.node('w1'), node);
  assert.ok(bubble(el, 'w1').includes('chat two'));
  assert.equal(el._messages[0].trim, 'wanting');
  // The new chat's mail is its own, and asked for at its own URL.
  const r2 = await ask(el, 'w1', trimOf('tw1', enc2));
  r2.answer({ state: 'queued' }, 202);
  await settle();
});

await ok('the element leaving lets the observer go; an answer after it does nothing', async () => {
  const el = await open();
  const req = await ask(el);
  const node = el._thread.node('w1');
  el.disconnectedCallback();
  assert.ok(observers.every((o) => !o.live));
  req.answer({ state: 'queued' }, 202);
  await settle();
  assert.equal(el._thread.node('w1'), node);
  assert.ok(!bubble(el, 'w1').includes('trim-state'));
});

await ok('a chat that is gone stops asking; a status line it showed goes', async () => {
  const el = await open();
  const req = await ask(el);
  req.answer({ state: 'queued' }, 202);
  await settle();
  assert.ok(bubble(el, 'w1').includes(TIDY));
  el._stopPolling();
  assert.ok(!bubble(el, 'w1').includes('trim-state'));
  assert.equal(watched().length, 0);
});

// ── The reader's place ───────────────────────────────────────────────────────
// DAY 0–30, big 30–490 (20 lines), a 490–570, b … g (80 each): 1050 high.
const TALL = [
  mail('big', { trim: 'wanting', text: lines(20) }),
  mail('a'), mail('b'), mail('c'), mail('d'), mail('e'), mail('f'), mail('g'),
];
await ok('a bubble above the view that shrinks leaves the reader where they were', async () => {
  const el = await open({}, TALL);
  const t = el._thread;
  t.scrollTop = 100;                       // big fills the view
  const req = await ask(el, 'big');
  t.scrollTop = 500;                       // read on: big ends at -10, a at -10..70
  const aTop = t.rectOf(t.node('a')).top;
  req.answer({ state: 'model', text: 'Short' });
  await settle();
  assert.equal(t.rectOf(t.node('a')).top, aTop, 'the message on screen keeps its offset');
  assert.ok(t.scrollTop < 500, 'the offset moved by what went above it');
});

await ok('a reader at the bottom stays at the bottom', async () => {
  const el = await open({}, [mail('a'), mail('b'), mail('c'), mail('big', { trim: 'wanting', text: lines(20) })]);
  const t = el._thread;
  t.scrollTop = t.scrollHeight;
  const req = await ask(el, 'big');
  req.answer({ state: 'running' }, 202);
  await settle();
  assert.equal(t.scrollTop, t.scrollHeight - t.clientHeight, 'the status line does not leave the bottom');
  await poll(el, [mail('a'), mail('b'), mail('c'), mail('big', { trim: 'model', text: 'Short' })]);
  assert.equal(t.scrollTop, t.scrollHeight - t.clientHeight, 'nor does the shorter text');
});

// ── The Original ─────────────────────────────────────────────────────────────
const NOTE = '<p class="orig-note">Shortened by a model &mdash; this is the full mail.</p>';
await ok('an open Original survives the text changing under it, and says a model shortened it', async () => {
  const el = await open();
  const req = await ask(el);
  req.answer({ state: 'queued' }, 202);
  await settle();
  el._toggleOriginal('w1');
  take((w) => w.method === 'GET' && w.url === orig('tw1'), 'the original').answer(DOC('tw1'));
  await settle();
  await settle();
  assert.ok(bubble(el, 'w1').includes('orig-open'));
  assert.ok(!bubble(el, 'w1').includes('orig-note'), 'not yet shortened');
  assert.ok(!bubble(el, 'w1').includes('trim-state'), 'the panel in place of the text, no status line');
  await poll(el, with_('w1', { trim: 'model', text: 'Short' }));
  const html = bubble(el, 'w1');
  assert.ok(html.includes('orig-open'), 'still open');
  assert.ok(html.includes(`<div class="orig">${NOTE}`), 'the panel is headed by the one line');
  assert.equal(el._thread.node('w1')._slot.children.length, 1, 'its frame mounted again');
  assert.equal(log.filter((r) => r.url === orig('tw1')).length, 1, 'not fetched again');
  // Closed: the bubble shows the shortened text, and says nothing more.
  el._toggleOriginal('w1');
  assert.ok(bubble(el, 'w1').includes('Short') && !bubble(el, 'w1').includes('orig-note'));
});

await ok('only a mail a model shortened carries the line', async () => {
  const el = await open();
  for (const [mid, has] of [['mdl', true], ['d1', false], ['w2', false]]) {
    el._toggleOriginal(mid);
    take((w) => w.method === 'GET' && w.url === orig(`t${mid}`), 'the original').answer(DOC(`t${mid}`));
    await settle();
    await settle();
    assert.equal(bubble(el, mid).includes(NOTE), has, mid);
  }
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def static_checks() -> list[str]:
    """What the source itself must hold to, whether or not Node is here."""
    errors = []
    with open(CHAT_PAGE, encoding="utf-8") as fh:
        src = fh.read()
    # A full render puts the watch on the new nodes.
    render = re.search(r"\n  render\(\) \{(.*?)\n  \}\n", src, re.S)
    if not render or "this._trimSweep()" not in render.group(1):
        errors.append("render() no longer sweeps the trim's watch")
    # The request URL is the Original's, its last segment named again —
    # never a token the page put together.
    if "trimUrl(ownOriginal(" not in src:
        errors.append("the trim request no longer goes to the accepted Original's sibling")
    # The observer is made in one place, behind the mail-only sweep.
    if src.count("new IntersectionObserver(") != 1:
        errors.append("chat-page.js makes an IntersectionObserver outside _trimSweep")
    return errors


def main() -> int:
    errors = static_checks()
    if errors:
        for e in errors:
            print("FAIL: " + e)
        return 1
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the mail trim test needs it")
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
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True,
                              text=True, timeout=120)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: mail trim test")
            return 1
    print("PASS: mail trim test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
