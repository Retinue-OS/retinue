#!/usr/bin/env python3
"""Writing mail in the dashboard (webapp/components/chat-page.js, chats.js,
compose-sheet.js and swipe-rows.js; docs/mail-chats.md, "Writing").

The elements are driven under Node with a scripted DOM and a scripted fetch,
and pin:

  * whom a reply reaches (replyRecipients): the latest live inbound mail's
    sender To and everyone else on it in Cc, the user's own addresses left
    out by the account and by the participant list while it is complete;
    Sender only drops the Cc; a thread whose latest mail is the user's own,
    or that is only a queued mail, goes to that mail's recipients again;
  * the composer on a mail chat: the To/Cc line, Reply all / Sender only in
    a group thread whose reply to all reaches more than the sender (and not
    elsewhere), the placeholder, labels and send label worded for mail;
    messenger composers as before;
  * the reply choice in the request body (`reply`, "all" by default and in a
    1:1, none on a messenger), reset with each sent mail;
  * Enter is a line break, Ctrl+Enter and ⌘+Enter send, an input method's
    Enter does not — on a mail chat and on a messenger chat alike;
  * the pending bubble's whole life: from a 202 at once (the optimistic
    bubble gives way, nothing remembered as seen or unconfirmed, a note in
    the live region), kept by a poll that left before it, from `pending` on
    a load, the same node across polls, redrawn in place when it changes,
    new messages going in above it, replaced by the mail it became (matched
    by Message-ID, focus moved there), removed when rejected (focus to the
    thread), never absorbed by a message with the same words; its accessible
    name and visible state; its text escaped; a page left mid-send does not
    put a queued mail back in the box;
  * the approval link: built by DOM calls, only from a path that is exactly
    /sends/<slug>/<id> — anything else plain text;
  * an unconfirmed mail send reconciled by its Message-ID, not its words; a
    refused mail says why;
  * ＋ on the Chats page: present only when GET /chats/compose offers
    something, its menu, the sheet it opens; the delete wording of a mail
    thread (the mails move to the mailbox's Trash) and of a messenger chat;
    a pending preview in the list;
  * the compose sheet: address shape and splitting, chips from typing and
    pasting, invalid input left in place with its error, removal, address
    book suggestions with the keyboard (stale answers dropped), validation
    before any request, the request, navigation to the new chat on 200 and
    on 202, errors in place, Escape and the focus given back, the page
    behind inert, the unsent mail kept for the next opening;
  * the quick-pattern prompts worded for mail.

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
WEBAPP = os.path.join(HERE, "..", "webapp")
COMPONENTS = os.path.join(WEBAPP, "components")

HARNESS = r"""
import assert from 'node:assert/strict';

// ── A scripted DOM ───────────────────────────────────────────────────────────
// The thread is an ordered list of top-level nodes, each the HTML of one
// <div> the page put there; enough of a node for what the page asks of a
// bubble, a separator and a pending bubble (its link slot).
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
const unesc = (s) => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
  .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
class Anchor {
  constructor() { this.attrs = new Map(); this.className = ''; this.textContent = ''; }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  getAttribute(n) { return this.attrs.has(n) ? this.attrs.get(n) : null; }
  focus() { globalThis.focused = this; document.activeElement = this; }
}
class Slot {
  constructor() { this.children = []; this.textContent = ''; }
  appendChild(c) { this.children.push(c); return c; }
}
class Node {
  constructor(thread, html) {
    this.thread = thread;
    this.html = html;
    const open = html.slice(0, html.indexOf('>') + 1);
    const attr = (n) => { const m = new RegExp(`${n}="([^"]*)"`).exec(open); return m ? unesc(m[1]) : null; };
    this.mid = attr('data-mid');
    this.pend = attr('data-pending');
    this.cls = (attr('class') || '').split(/\s+/);
    this.isRow = open.includes('data-mark-read-row');
    this.isDay = open.includes('class="day-sep"');
    this.isEmpty = open.includes('class="center');
    this.attrs = new Map();
    this._slot = null;
  }
  get classList() { return { contains: (c) => this.cls.includes(c), add() {}, remove() {} }; }
  get previousElementSibling() { const i = this.thread.nodes.indexOf(this); return i > 0 ? this.thread.nodes[i - 1] : null; }
  get nextElementSibling() { const i = this.thread.nodes.indexOf(this); return this.thread.nodes[i + 1] || null; }
  getAttribute(n) {
    if (n === 'data-mid') return this.mid;
    if (n === 'data-pending') return this.pend;
    return this.attrs.has(n) ? this.attrs.get(n) : null;
  }
  hasAttribute(n) { return (n === 'data-pending' && this.pend !== null) || this.attrs.has(n); }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  matches() { return false; }
  focus(opts) { globalThis.focused = this; globalThis.focusOpts = opts; document.activeElement = this; }
  getBoundingClientRect() { return { top: 0, bottom: 10 }; }
  querySelector(sel) {
    if (sel === '[data-approve-slot]') return this.html.includes('data-approve-slot') ? (this._slot = this._slot || new Slot()) : null;
    if (sel === '.pend-link') return this._slot && this._slot.children.find((c) => c.className === 'pend-link') || null;
    return null;
  }
  contains(x) { return !!x && !!this._slot && this._slot.children.includes(x); }
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
    this.scrollTop = 0;
    this.scrollHeight = 1000;
    this.clientHeight = 300;
    this.attrs = new Map();
  }
  hasAttribute(n) { return this.attrs.has(n); }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  focus() { globalThis.focused = this; document.activeElement = this; }
  getBoundingClientRect() { return { top: 0, bottom: 300 }; }
  querySelector(sel) {
    if (sel === '[data-pending]') return this.nodes.find((n) => n.pend !== null) || null;
    if (sel === ':scope > .center') return this.nodes.find((n) => n.isEmpty) || null;
    if (sel === '[data-mark-read-row]') return this.nodes.find((n) => n.isRow) || null;
    return null;
  }
  querySelectorAll(sel) {
    if (sel === '[data-mid]') return this.nodes.filter((n) => n.mid);
    if (sel === '[data-pending]') return this.nodes.filter((n) => n.pend !== null);
    if (sel === '.day-sep, .unread-sep') return this.nodes.filter((n) => n.isDay || n.cls.includes('unread-sep'));
    return [];
  }
  insertAdjacentHTML(where, html) {
    assert.equal(where, 'beforeend');
    this.nodes.push(...chunks(html).map((h) => new Node(this, h)));
  }
  node(mid) { return this.nodes.find((n) => n.mid === mid); }
  pnode(rid) { return this.nodes.find((n) => n.pend === rid); }
  // Messages by id, pending bubbles as "P:<request id>", the empty line.
  layout() { return this.nodes.map((n) => (n.mid ? n.mid : n.pend !== null ? `P:${n.pend}` : n.isEmpty ? 'EMPTY' : null)).filter(Boolean); }
}

// A tiny element tree for the composer and the compose sheet: nodes made on
// demand per selector, recording what is set on them and what listens.
class El {
  constructor(sel = '') {
    this.sel = sel; this.attrs = new Map(); this.listeners = {}; this.value = ''; this.innerHTML = '';
    this.textContent = ''; this.hidden = false; this.disabled = false; this.style = {}; this.checked = false;
    this.scrollHeight = 20; this.clientHeight = 20; this.dataset = {};
    const cls = new Set();
    this.classList = { toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)), add: (c) => cls.add(c),
                       remove: (c) => cls.delete(c), contains: (c) => cls.has(c) };
  }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener() {}
  fire(t, e = {}) {
    const ev = { type: t, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
                 stopPropagation() {}, ...e };
    for (const f of this.listeners[t] || []) f(ev);
    return ev;
  }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  getAttribute(n) { return this.attrs.has(n) ? this.attrs.get(n) : null; }
  hasAttribute(n) { return this.attrs.has(n); }
  removeAttribute(n) { this.attrs.delete(n); }
  focus() { globalThis.focused = this; if (this.root) this.root.activeElement = this; document.activeElement = this; }
  blur() {}
  requestSubmit() { this.submitted = (this.submitted || 0) + 1; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  setSelectionRange() {}
}
class Root {
  constructor() { this.nodes = new Map(); this.listeners = {}; this._html = ''; this.activeElement = null; }
  set innerHTML(h) { this._html = h; this.nodes.clear(); }
  get innerHTML() { return this._html; }
  // A node per selector, carrying the attribute the selector names (so the
  // element's delegated handlers recognise it as their target).
  querySelector(sel) {
    if (!this.nodes.has(sel)) {
      const e = new El(sel);
      e.root = this;
      const m = /^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(sel);
      if (m) e.setAttribute(m[1], m[2] === undefined ? '' : m[2]);
      this.nodes.set(sel, e);
    }
    return this.nodes.get(sel);
  }
  querySelectorAll() { return []; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener() {}
  fire(t, target, e = {}) {
    const ev = { type: t, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
                 stopPropagation() {}, ...e };
    for (const f of this.listeners[t] || []) f(ev);
    return ev;
  }
}

const registry = new Map();
class FakeElement {
  constructor() { this._attrs = new Map(); this.shadowRoot = null; this.style = {}; this.dataset = {}; }
  attachShadow() { this.shadowRoot = new Root(); return this.shadowRoot; }
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
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
const bodyKids = [];
const main = { inert: false };
let sheetEl = null;
globalThis.document = {
  addEventListener() {}, removeEventListener() {},
  createElement: (tag) => {
    if (tag === 'a') return new Anchor();
    if (tag === 'template') {
      return { set innerHTML(h) { const c = chunks(h); this.content = { firstElementChild: c.length ? new Node(null, c[0]) : null }; } };
    }
    if (registry.has(tag)) return new (registry.get(tag))();
    return new FakeElement();
  },
  querySelector: (sel) => (sel === 'retinue-compose-sheet' ? sheetEl : null),
  getElementById() { return null; },
  hidden: false, visibilityState: 'visible', activeElement: null,
  body: { children: bodyKids, appendChild(c) { bodyKids.push(c); if (c.constructor === registry.get('retinue-compose-sheet')) sheetEl = c; c.connectedCallback && c.connectedCallback(); } },
  documentElement: { dataset: {} }, title: 'Retinue — Chat',
};
bodyKids.push(main);
globalThis.window = globalThis;
globalThis.innerHeight = 800;
const assigned = [];
globalThis.location = { hash: '', pathname: '/chat.html', search: '', href: 'http://dash/chat.html',
                        replace() {}, assign: (u) => assigned.push(String(u)) };
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.dispatchEvent = () => true;
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms, ...rest) => (ms >= 150 ? 0 : realSetTimeout(fn, ms, ...rest));
globalThis.history = { length: 1, pushState() {}, replaceState() {}, back() {} };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
const store = new Map();
globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
globalThis.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', mediaDevices: null }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };
globalThis.URL.revokeObjectURL = () => {};

// ── A scripted gateway ───────────────────────────────────────────────────────
const wire = [];
globalThis.fetch = (url, init = {}) => {
  const req = { url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null };
  return new Promise((resolve, reject) => {
    req.answer = (doc, status = 200) => resolve({ ok: status >= 200 && status < 300, status, json: async () => doc });
    req.fail = () => reject(new TypeError('offline'));
    wire.push(req);
  });
};
const settle = () => new Promise((r) => realSetTimeout(r, 0));
const take = (pred, what) => {
  const i = wire.findIndex(pred);
  assert.ok(i >= 0, `expected ${what}, saw ${JSON.stringify(wire.map((w) => [w.method, w.url]))}`);
  return wire.splice(i, 1)[0];
};

const page = await import('./chat-page.js');
const { replyRecipients, approvalPath, QUICK_PATTERNS } = page;
const Page = registry.get('retinue-chat-page');
const List = registry.get('retinue-chats');
const sheetMod = await import('./compose-sheet.js');
const { isAddress, splitAddresses } = sheetMod;
const { SwipeRows } = await import('./swipe-rows.js');
const { previewHtml } = await import('./chats.js');
const { shelf } = await import('./shelf-store.js');
shelf.observe = () => {};

let passed = 0;
async function ok(name, fn) { await fn(); passed += 1; console.log('  ok  ' + name); }

// ── Fixtures ─────────────────────────────────────────────────────────────────
const ACCT = 'me@example.org';
const ID = 'email:~me@example.org:root@mail.example.com';
const MSG_URL = `/chats/${encodeURIComponent(ID)}/messages`;
const PEOPLE = [{ address: 'ann@example.com', name: 'Ann Example', contact: null },
                { address: 'bob@builder.example', name: 'Bob Builder', contact: null },
                { address: 'carla@example.net', name: null, contact: null }];
const summary = (extra = {}) => ({
  id: ID, channel: 'email', account: ACCT, key: 'root@mail.example.com', name: 'Quarterly numbers', group: true,
  participants: PEOPLE, participants_more: 0, peer: { address: 'ann@example.com', name: 'Ann Example' },
  unread: 0, list_group: 'read', last_read: null, inbound_seq: 3, archived: false, muted: false, starred: false,
  companion: null, instructed: null, draft: null, messages: MSG_URL, can_send: true, ...extra,
});
const mail = (id, direction, ts, extra = {}) => ({
  id, chat: ID, direction, ts, text: `text of ${id}`, sender: 'ann@example.com', sender_name: 'Ann Example',
  subject: 'Re: Quarterly numbers', recipients: [ACCT, 'bob@builder.example'], cc: ['carla@example.net'],
  trim: 'deterministic', removed: false, removed_at: null, attachments: [], ...extra,
});
const BASE = [
  mail('m1', 'in', '2026-11-20T08:00:00Z', { subject: 'Quarterly numbers' }),
  mail('m2', 'out', '2026-11-20T08:05:00Z', { author: 'user', sender: ACCT, recipients: ['ann@example.com'], cc: [] }),
  mail('m3', 'in', '2026-11-20T09:00:00Z'),
];
const pend = (rid, extra = {}) => ({
  request_id: rid, approval_url: `/sends/mail-gateway/${rid}`, text: `queued ${rid}`, ts: '2026-11-20T10:00:00Z',
  author: 'user', to: ['ann@example.com'], cc: ['bob@builder.example'], subject: 'Re: Quarterly numbers', ...extra,
});
const clone = (x) => JSON.parse(JSON.stringify(x));

// The chat page with its thread: render() rebuilds the thread from
// _chatThreadHtml and mounts the approval links, as the real one does.
function rig(el) {
  el.render = function () {
    const thread = this._state === 'ok' ? new Thread(this._chatThreadHtml()) : null;
    this._thread = thread;
    this.shadowRoot = {
      activeElement: null,
      querySelector: (s) => {
        if (s === '[data-chat-thread]') return thread;
        const m = /^\[data-chat-thread\] \[data-mid="(.*)"\]$/.exec(s);
        if (m && thread) return thread.node(m[1]) || null;
        return null;
      },
      querySelectorAll: () => [],
    };
    if (thread) this._mountApprovals();
  };
  el._notes = [];
  el._showNote = (h) => el._notes.push(h);
  el._refreshChatComposer = () => {};
  el._keepLocal = () => {};
}
async function open(chat = {}, messages = BASE, pending = []) {
  wire.length = 0;
  document.activeElement = null;
  globalThis.focused = null;
  const el = new Page();
  el.attachShadow({ mode: 'open' });
  el._id = chat.id || ID;
  rig(el);
  const loading = el._load();
  take((w) => w.url === '/chats', 'the list').answer({ chats: [summary(chat)] });
  await settle();
  take((w) => w.url === (chat.messages || MSG_URL), 'the messages')
    .answer({ chat: summary(chat), messages: clone(messages), pending: clone(pending) });
  await loading;
  clearInterval(el._pollTimer);
  return el;
}
async function poll(el, messages, pending, chat = {}) {
  const p = el._poll();
  take((w) => w.url === el._msgUrl && w.method === 'GET', 'the poll')
    .answer({ chat: summary(chat), messages: clone(messages), ...(pending === undefined ? {} : { pending: clone(pending) }) });
  await p;
}
async function send(el, text, answer, status = 200) {
  const p = el._sendChat(text);
  await settle();
  const req = take((w) => w.method === 'POST' && w.url.endsWith('/send'), 'the send');
  req.answer(answer, status);
  await p;
  return req;
}
const link = (el, rid) => {
  const n = el._thread.pnode(rid);
  assert.ok(n, `pending bubble ${rid}`);
  return n._slot;
};

// ── Whom a reply reaches ─────────────────────────────────────────────────────
await ok('a reply goes to the latest inbound mail\'s sender, the rest in Cc', async () => {
  const c = summary();
  let r = replyRecipients(c, BASE);
  assert.deepEqual(r.to, [{ address: 'ann@example.com', name: 'Ann Example' }]);
  assert.deepEqual(r.cc, [{ address: 'bob@builder.example', name: 'Bob Builder' },
                          { address: 'carla@example.net', name: '' }], 'the account is left out');
  assert.deepEqual(replyRecipients(c, BASE, 'sender').cc, [], 'Sender only');
  // An alias of the user's on the mail: not a participant, so not Cc'd while
  // the participant list is complete …
  const alias = [mail('a', 'in', 't', { recipients: ['ME-alias@example.org', 'bob@builder.example'], cc: [] })];
  assert.deepEqual(replyRecipients(c, alias).cc.map((x) => x.address), ['bob@builder.example']);
  // … and only the account is known to be the user's once it is capped.
  assert.deepEqual(replyRecipients({ ...c, participants_more: 2 }, alias).cc.map((x) => x.address),
                   ['ME-alias@example.org', 'bob@builder.example']);
  // Case and repeats: one entry each; the To is never Cc'd as well.
  const dup = [mail('d', 'in', 't', { recipients: ['Ann@Example.com', ACCT.toUpperCase(), 'bob@builder.example'], cc: ['BOB@builder.example'] })];
  r = replyRecipients(c, dup);
  assert.deepEqual(r.cc.map((x) => x.address), ['bob@builder.example']);
  // A tombstone is not replied to; the newest live inbound is.
  const gone = [...BASE, mail('g', 'in', 't', { sender: 'carla@example.net', removed: true })];
  assert.equal(replyRecipients(c, gone).to[0].address, 'ann@example.com');
  // The sender's own name where the participants give none.
  const named = [mail('n', 'in', 't', { sender: 'zed@x.org', sender_name: 'Zed', recipients: [ACCT], cc: [] })];
  assert.deepEqual(replyRecipients({ ...c, participants: [] }, named).to, [{ address: 'zed@x.org', name: 'Zed' }]);
});

await ok('the user\'s own mail last, or only a queued one: its recipients again', async () => {
  const c = summary();
  const own = [mail('o', 'out', 't', { sender: ACCT, recipients: ['ann@example.com'], cc: ['carla@example.net'] })];
  assert.deepEqual(replyRecipients(c, own).to.map((x) => x.address), ['ann@example.com']);
  assert.deepEqual(replyRecipients(c, own).cc.map((x) => x.address), ['carla@example.net']);
  assert.deepEqual(replyRecipients(c, own, 'sender').cc, []);
  assert.deepEqual(replyRecipients(c, []), { to: [], cc: [] }, 'nothing to reply to');
  const r = replyRecipients(c, [], 'all', [pend('p1', { to: ['ann@example.com'], cc: ['bob@builder.example'] })]);
  assert.deepEqual([r.to.map((x) => x.name), r.cc.map((x) => x.name)], [['Ann Example'], ['Bob Builder']]);
  // The page's own optimistic bubble is not a mail of the thread.
  const local = [...own, { id: 'local-1', direction: 'out', text: 'x', recipients: ['nobody@x.org'] }];
  assert.equal(replyRecipients(c, local).to[0].address, 'ann@example.com');
});

// ── The composer ─────────────────────────────────────────────────────────────
await ok('a mail composer: To/Cc line, Reply all / Sender only, mail wording', async () => {
  const el = await open();
  const h = el._chatComposerHtml();
  assert.ok(h.includes('<div class="to-line mail" data-to-line'), h);
  assert.ok(h.includes('To <b>Ann Example</b> &middot; Cc <b>Bob Builder, carla@example.net</b>'), h);
  assert.ok(h.includes('title="Your reply goes to Ann Example &lt;ann@example.com&gt;, with a copy to Bob Builder &lt;bob@builder.example&gt;, carla@example.net — by e-mail, not to Ara."'), h);
  assert.ok(h.includes('role="radiogroup" aria-label="Who the reply goes to"'));
  assert.ok(h.includes('value="all" data-reply-mode checked> Reply all'), h);
  assert.ok(h.includes('value="sender" data-reply-mode> Sender only'), h);
  assert.ok(h.includes('placeholder="Write your reply …"'));
  assert.ok(h.includes('aria-label="Your reply — Enter starts a new line, Ctrl+Enter or ⌘+Enter sends"'));
  assert.ok(h.includes('enterkeyhint="enter"'));
  assert.ok(h.includes('aria-label="Send the reply to Ann Example (Ctrl+Enter or ⌘+Enter)"'), h);
  el._setReplyMode('sender');
  const s = el._chatComposerHtml();
  assert.ok(s.includes('To <b>Ann Example</b></div>'), 'no Cc for the sender only');
  assert.ok(s.includes('value="sender" data-reply-mode checked'));
  // A 1:1 thread, and a group thread whose reply to all reaches the sender
  // alone: no choice to make.
  const solo = await open({ group: false });
  assert.ok(!solo._chatComposerHtml().includes('data-reply-mode'));
  const lone = await open({}, [mail('x', 'in', 't', { recipients: [ACCT], cc: [] })]);
  assert.ok(!lone._chatComposerHtml().includes('data-reply-mode'));
  assert.ok(lone._chatComposerHtml().includes('To <b>Ann Example</b></div>'));
  // Nothing left to reply to: the participants, as before.
  const empty = await open({}, [mail('g', 'in', 't', { removed: true, text: '' })]);
  assert.ok(empty._chatComposerHtml().includes('To <b>Ann Example, Bob Builder, carla@example.net</b> &middot; E-mail'));
});

await ok('a messenger composer is as it was', async () => {
  const SID = 'signal:~+41000:+41999';
  const el = await open({ id: SID, channel: 'signal', account: '+41000', key: '+41999', name: 'Mara', group: false,
                          participants: undefined, participants_more: undefined, peer: undefined,
                          messages: '/chats/s/messages' },
                        [{ id: 'a', chat: SID, direction: 'in', text: 'hi', ts: '2026-11-20T08:00:00Z', sender: '+41999' }]);
  const h = el._chatComposerHtml();
  assert.ok(h.includes('placeholder="Message Mara …" aria-label="Message Mara …" autocomplete="off"'), h);
  assert.ok(h.includes('To <b>Mara</b> &middot; Signal</div>'));
  assert.ok(h.includes('aria-label="Send to Mara"'));
  for (const no of ['data-reply-mode', 'enterkeyhint', 'Write your reply']) assert.ok(!h.includes(no), no);
  // And a send carries no reply choice.
  const req = await send(el, 'yo', { id: 'b', chat: SID, direction: 'out', author: 'user', text: 'yo', ts: '2026-11-20T08:01:00Z' });
  assert.deepEqual(req.body, { text: 'yo' });
});

await ok('the reply choice rides the request and resets with each sent mail', async () => {
  const el = await open();
  let req = await send(el, 'one', { id: 's1', chat: ID, direction: 'out', author: 'user', text: 'one', ts: '2026-11-20T11:00:00Z' });
  assert.deepEqual(req.body, { text: 'one', reply: 'all' });
  el._setReplyMode('sender');
  req = await send(el, 'two', { id: 's2', chat: ID, direction: 'out', author: 'user', text: 'two', ts: '2026-11-20T11:01:00Z' });
  assert.equal(req.body.reply, 'sender');
  assert.equal(el._replyMode, 'all', 'reset after the send');
  // Queued counts as sent for the choice; a failure keeps it.
  el._setReplyMode('sender');
  await send(el, 'three', { pending: pend('q1') }, 202);
  assert.equal(el._replyMode, 'all');
  el._setReplyMode('sender');
  await send(el, 'four', { error: 'down' }, 502);
  assert.equal(el._replyMode, 'sender');
  // A 1:1 thread says "all": the sender, the only one there is.
  const solo = await open({ group: false });
  solo._replyMode = 'sender';
  req = await send(solo, 'x', { id: 's3', chat: ID, direction: 'out', author: 'user', text: 'x', ts: '2026-11-20T11:02:00Z' });
  assert.equal(req.body.reply, 'all');
});

await ok('Enter breaks a line; Ctrl+Enter and ⌘+Enter send; an IME\'s Enter does not', async () => {
  for (const chat of [{}, { id: 'signal:~+41000:+41999', channel: 'signal', group: false, participants: undefined,
                            messages: '/chats/s/messages' }]) {
    const el = await open(chat, chat.channel ? [] : BASE);
    const form = new El('form');
    const input = new El('textarea');
    const field = new El('field');
    form.querySelector = (s) => (s === 'textarea' ? input : s === '[data-field]' ? field : null);
    form.querySelectorAll = () => [];
    el.shadowRoot = { querySelector: (s) => (s === '[data-composer="chat"]' ? form : null), querySelectorAll: () => [],
                      activeElement: null };
    const sent = [];
    el._sendChat = (t) => sent.push(t);
    el._wireComposer('chat');
    let ev = input.fire('keydown', { key: 'Enter' });
    assert.ok(!ev.defaultPrevented && !form.submitted, 'Enter is the textarea\'s own');
    ev = input.fire('keydown', { key: 'Enter', shiftKey: true });
    assert.ok(!ev.defaultPrevented && !form.submitted);
    ev = input.fire('keydown', { key: 'Enter', ctrlKey: true, isComposing: true });
    assert.ok(!form.submitted, 'an input method commits, it does not send');
    ev = input.fire('keydown', { key: 'Enter', ctrlKey: true });
    assert.ok(ev.defaultPrevented && form.submitted === 1, 'Ctrl+Enter');
    input.fire('keydown', { key: 'Enter', metaKey: true });
    assert.equal(form.submitted, 2, '⌘+Enter');
    // The submit itself sends what the box holds, lines and all.
    input.value = 'line one\nline two';
    form.fire('submit');
    assert.deepEqual(sent, ['line one\nline two']);
  }
});

// ── The pending bubble ───────────────────────────────────────────────────────
await ok('a queued send: the pending bubble at once, with its link and a word for assistive tech', async () => {
  const el = await open();
  el._draftSaved = 'the words';
  await send(el, 'Friday <b>works</b> https://example.org/x', { pending: pend('q1', { text: 'Friday <b>works</b> https://example.org/x' }) }, 202);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q1'], 'the optimistic bubble gave way');
  assert.ok(!el._messages.some((m) => String(m.id).startsWith('local-')));
  assert.ok(!el._seen.has('q1') && el._unconfirmed.length === 0, 'not a message, not unconfirmed');
  assert.equal(el._draftSaved, '', 'the shared draft is spent');
  const n = el._thread.pnode('q1');
  assert.ok(n.html.startsWith('<div class="msg out pending by-user" data-pending="q1" role="group" ' +
    'aria-label="Waiting for approval: your mail to ann@example.com">'), n.html);
  assert.ok(n.html.includes('<small class="who">You &middot; not sent yet</small>'));
  assert.ok(n.html.includes('<span class="pend-badge">Waiting for approval</span>'));
  assert.ok(n.html.includes('Friday &lt;b&gt;works&lt;/b&gt; <a href="https://example.org/x" target="_blank" rel="noopener noreferrer">'), n.html);
  assert.ok(!n.html.includes('<a class="pend-link'), 'the link is no markup');
  const a = link(el, 'q1').children[0];
  assert.equal(a.getAttribute('href'), '/sends/mail-gateway/q1');
  assert.equal(a.className, 'pend-link');
  assert.equal(a.textContent, 'Approve or reject');
  assert.ok(el._notes.some((t) => t.includes('waits for your approval')), 'said in the live region');
  // An agent's queued mail is labelled as theirs.
  const agent = el._pendingHtml(pend('q9', { author: 'agent', agent: 'Ara' }));
  assert.ok(agent.includes('by-agent') && agent.includes('aria-label="Waiting for approval: Ara&#39;s mail'), agent);
  // A 202 that names no entry is still a queued send: the bubble comes down,
  // and the chat's own list is asked at once.
  const bare = await open();
  const p = bare._sendChat('hi');
  await settle();
  take((w) => w.method === 'POST' && w.url.endsWith('/send'), 'the send').answer({ status: 'pending_approval' }, 202);
  await p;
  assert.deepEqual(bare._thread.layout(), ['m1', 'm2', 'm3']);
  take((w) => w.method === 'GET' && w.url === MSG_URL, 'the poll that brings it')
    .answer({ chat: summary(), messages: clone(BASE), pending: [pend('q3', { text: 'hi' })] });
  await settle();
  await settle();
  assert.deepEqual(bare._thread.layout(), ['m1', 'm2', 'm3', 'P:q3']);
});

await ok('the bubble lives through polls and reloads, and leaves when it is decided', async () => {
  const el = await open();
  // A poll leaves, the send is queued, the poll answers without it.
  const early = el._poll();
  const earlyReq = take((w) => w.url === MSG_URL, 'the early poll');
  await send(el, 'hello', { pending: pend('q1', { text: 'hello' }) }, 202);
  earlyReq.answer({ chat: summary(), messages: clone(BASE), pending: [] });
  await early;
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q1'], 'a poll from before it cannot take it down');
  // Listed: the same node.
  const node = el._thread.pnode('q1');
  await poll(el, BASE, [pend('q1', { text: 'hello' })]);
  assert.equal(el._thread.pnode('q1'), node);
  // A document that says nothing about pending sends changes nothing.
  await poll(el, BASE, undefined);
  assert.equal(el._thread.pnode('q1'), node);
  // Another one queued elsewhere (the companion, mail-push.py) joins after it;
  // a new mail goes in above both.
  const m4 = mail('m4', 'in', '2026-11-20T12:00:00Z', { sender: 'bob@builder.example', sender_name: 'Bob Builder' });
  await poll(el, [...BASE, m4], [pend('q1', { text: 'hello' }), pend('q2', { author: 'agent' })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'm4', 'P:q1', 'P:q2']);
  assert.ok(el._thread.pnode('q2').html.includes('by-agent'));
  // Changed words: redrawn in place, the link again.
  await poll(el, [...BASE, m4], [pend('q1', { text: 'hello again' }), pend('q2', { author: 'agent' })]);
  assert.notEqual(el._thread.pnode('q1'), node);
  assert.ok(el._thread.pnode('q1').html.includes('hello again'));
  assert.equal(link(el, 'q1').children[0].getAttribute('href'), '/sends/mail-gateway/q1');
  assert.deepEqual(el._thread.layout().slice(-2), ['P:q1', 'P:q2']);
  // A reload: both are there, from `pending`.
  const again = await open({}, [...BASE, m4], [pend('q1'), pend('q2', { author: 'agent' })]);
  assert.deepEqual(again._thread.layout(), ['m1', 'm2', 'm3', 'm4', 'P:q1', 'P:q2']);
  assert.equal(link(again, 'q2').children.length, 1);
  // Rejected while its link had focus: it goes, focus to the thread.
  document.activeElement = link(again, 'q2').children[0];
  await poll(again, [...BASE, m4], [pend('q1')]);
  assert.deepEqual(again._thread.layout(), ['m1', 'm2', 'm3', 'm4', 'P:q1']);
  assert.equal(globalThis.focused, again._thread, 'focus does not fall to the document');
});

await ok('a released send gives way to the mail it became — by Message-ID, never by words', async () => {
  const el = await open({}, BASE, [pend('q1', { text: 'same words', message_id: 'sent-1@me.example.org' })]);
  // A message with the very same words but another id: the bubble stays.
  const lookalike = mail('other@x', 'out', '2026-11-20T10:30:00Z', { text: 'same words', author: 'device', sender: ACCT });
  await poll(el, [...BASE, lookalike], [pend('q1', { text: 'same words', message_id: 'sent-1@me.example.org' })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'other@x', 'P:q1']);
  // Released: the mail arrives and the list may still lag — the bubble goes,
  // and the focus its link had moves to the mail.
  document.activeElement = link(el, 'q1').children[0];
  const sent = mail('sent-1@me.example.org', 'out', '2026-11-20T10:40:00Z', { text: 'same words', author: 'user', sender: ACCT });
  await poll(el, [...BASE, lookalike, sent], [pend('q1', { text: 'same words', message_id: 'sent-1@me.example.org' })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'other@x', 'sent-1@me.example.org']);
  assert.equal(globalThis.focused, el._thread.node('sent-1@me.example.org'));
  assert.equal(el._pending.length, 0);
  // A pending entry without a Message-ID simply leaves when it is no longer
  // listed, and its mail comes in as any message.
  const b = await open({}, BASE, [pend('q2')]);
  await poll(b, [...BASE, mail('s2', 'out', '2026-11-20T10:50:00Z', { text: 'queued q2', author: 'user', sender: ACCT })], []);
  assert.deepEqual(b._thread.layout(), ['m1', 'm2', 'm3', 's2']);
});

await ok('a new chat that is only a queued mail shows its bubble alone', async () => {
  const el = await open({}, [], [pend('q1', { to: ['zoe@x.org'], cc: [] })]);
  assert.deepEqual(el._thread.layout(), ['P:q1'], 'no "No messages." line');
  assert.ok(el._chatComposerHtml().includes('To <b>zoe@x.org</b>'));
  // Rejected: the thread is empty again.
  await poll(el, [], []);
  assert.deepEqual(el._thread.layout(), []);
  const none = await open({}, [], []);
  assert.deepEqual(none._thread.layout(), ['EMPTY']);
});

await ok('the approval link only for an exact /sends/<slug>/<id> path', async () => {
  const bad = ['https://evil.example/sends/mail-gateway/1', '//evil.example/sends/a/1', '/sends/mail-gateway/1?x=1',
               '/sends/mail-gateway/1#y', '/sends/../admin/1', '/sends/./x/1', '/sends/mail-gateway/..', '/sends/mail-gateway',
               '/sends/mail-gateway/1/approve', '/sends/mail%2Fgateway/1', 'javascript:alert(1)', '/sends/a b/1',
               ' /sends/a/1', '/sends/a/1 ', null, 42, '', `/sends/${'a'.repeat(200)}/1`];
  for (const u of bad) assert.equal(approvalPath(u), '', String(u));
  for (const u of ['/sends/mail-gateway/7c1e0b6a', '/sends/sms_1/AbC-9_x.y', '/sends/-/_']) assert.equal(approvalPath(u), u);
  const el = await open({}, BASE, bad.map((u, i) => pend(`b${i}`, { approval_url: u })));
  for (let i = 0; i < bad.length; i += 1) {
    const slot = link(el, `b${i}`);
    assert.equal(slot.children.length, 0, `no link for ${bad[i]}`);
    assert.equal(slot.textContent, 'Approve or reject it on the Sends page.');
  }
});

await ok('a page left mid-send does not put a queued mail back in the box', async () => {
  const el = await open({}, BASE, [pend('q1', { text: 'the long\nmail', ts: new Date().toISOString() })]);
  const s = { text: 'the long\nmail', at: new Date().toISOString() };
  assert.equal(el._sentLike(s), 1, 'the queued mail counts as gone');
  assert.equal(el._sentLike({ text: 'something else', at: s.at }), 0);
});

await ok('an unconfirmed mail send is found by its Message-ID, not its words', async () => {
  const el = await open();
  await send(el, 'my reply\n\n> quoted', { id: 'synth-1', unconfirmed: true, since: '2026-11-20T10:00:00Z',
                                         message_id: 'real-1@me.example.org', chat: ID, direction: 'out',
                                         author: 'user', text: 'my reply\n\n> quoted', ts: '2026-11-20T10:00:00Z' });
  assert.equal(el._unconfirmed[0].messageId, 'real-1@me.example.org');
  // The record's text is the trimmed text: other words, same mail.
  const rec = mail('real-1@me.example.org', 'out', '2026-11-20T10:00:05Z', { text: 'my reply', author: 'user', sender: ACCT });
  await poll(el, [...BASE, rec], []);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'real-1@me.example.org'], 'one bubble, not two');
  assert.equal(el._unconfirmed.length, 0);
});

await ok('a refused mail says why, and the words come back', async () => {
  const el = await open();
  let restored = '';
  el._restoreComposer = (t) => { restored = t; };
  await send(el, 'hello', { error: 'This thread has no mail left to reply to <here>.' }, 409);
  assert.equal(el._notes.at(-1), 'Not sent: This thread has no mail left to reply to &lt;here&gt;.');
  assert.equal(restored, 'hello');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  await send(el, 'again', { error: 'gateway down' }, 502);
  assert.equal(el._notes.at(-1), "Couldn't send &mdash; check the connection and try again.");
});

await ok('the quick patterns ask about mail on a mail chat', async () => {
  const by = (id) => QUICK_PATTERNS.find((p) => p.id === id);
  assert.equal(by('translate').prompt('', true), 'Translate the last mail for me, and draft a reply in the same language.');
  assert.equal(by('translate').prompt('', false), 'Translate the last message for me, and draft a reply in the same language.');
  assert.equal(by('summarize').prompt('', true), 'Summarize this mail thread since my last reply.');
  assert.equal(by('summarize').prompt('', false), 'Summarize this chat since my last reply.');
  assert.ok(by('propose').prompt('', true).includes('a mail that tells you to do something'));
  for (const id of ['shorter', 'warmer', 'formal']) {
    assert.ok(by(id).prompt('', true).includes('reply to the last mail'), id);
    assert.ok(by(id).prompt('draft', true).includes('\n\ndraft'), id);
  }
  // The page asks with the chat's kind.
  const el = await open();
  let asked = '';
  el._companionEl = () => ({ ask: (t) => { asked = t; } });
  el._setPane = () => {};
  el._quickPattern('translate');
  assert.equal(asked, by('translate').prompt('', true));
});

// ── The Chats page: ＋, Delete, the preview ──────────────────────────────────
const listChat = (id, extra = {}) => ({
  id, channel: 'email', account: ACCT, key: id, name: 'Quarterly numbers', group: true, participants: PEOPLE,
  participants_more: 0, unread: 0, list_group: 'archived', inbound_seq: 1, archived: true, muted: false,
  starred: false, last: { ts: '2026-11-20T10:00:00Z', direction: 'in', text: 'hi', sender_name: 'Ann Example' },
  draft: null, companion: null, messages: '/chats/x/messages', ...extra,
});
async function mountList(offers, chats = [listChat('email:~me@example.org:r1')]) {
  wire.length = 0;
  const el = new List();
  el.setAttribute('full', '');
  el.connectedCallback();
  clearInterval(el._timer);
  const compose = take((w) => w.url === '/chats/compose', 'GET /chats/compose');
  take((w) => w.url === '/chats', 'GET /chats').answer({ chats: clone(chats) });
  await settle();
  if (offers instanceof Error) compose.answer({}, 502);
  else if (offers === 404) compose.answer({}, 404);
  else compose.answer(offers);
  await settle();
  return el;
}

await ok('＋ is there when the server offers something to start, and opens the sheet', async () => {
  const el = await mountList({ channels: [{ channel: 'email', label: 'E-mail', accounts: [ACCT, 'work@example.com'] }] });
  const h = el.shadowRoot.innerHTML;
  assert.ok(h.includes('<div class="list-head"><div class="filter" role="tablist"'), h.slice(0, 300));
  assert.ok(h.includes('class="new-btn" data-new aria-expanded="false" aria-controls="chats-new" ' +
    'title="Start a new conversation" aria-label="Start a new conversation">&#65291;</button>'), h);
  assert.ok(h.includes('id="chats-new" data-new-menu hidden>'));
  assert.ok(h.includes('<button type="button" class="new-item" data-new-offer="0">New e-mail</button>'));
  // A refresh is not rendered under the open menu.
  el._newOpen = true;
  el._sig = '';
  el.load();
  take((w) => w.url === '/chats', 'GET /chats').answer({ chats: [] });
  await settle();
  assert.ok(el.shadowRoot.innerHTML.includes('data-new-offer'), 'the menu is not rebuilt away');
  el._newOpen = false;
  // Picking the offer opens the compose sheet for it.
  let back = 0;
  el.shadowRoot.querySelector = (s) => (s === '[data-new]' ? { focus: () => { back += 1; } } : null);
  el._toggleNew = () => {};
  el._startNew(0);
  assert.ok(sheetEl, 'the sheet is on the page');
  assert.deepEqual(sheetEl._offer, { channel: 'email', label: 'E-mail', accounts: [ACCT, 'work@example.com'] });
  assert.equal(sheetEl.hasAttribute('hidden'), false);
  sheetEl.close();
  assert.equal(back, 1, 'focus back on the ＋');
});

await ok('no ＋ without an offer; a gateway without the endpoint is not asked again', async () => {
  let el = await mountList({ channels: [] });
  assert.ok(!el.shadowRoot.innerHTML.includes('data-new'));
  assert.ok(!el.shadowRoot.innerHTML.includes('<div class="list-head">'), 'the tabs as before');
  el = await mountList(404);
  assert.deepEqual(el._compose, []);
  el.load();
  assert.ok(!wire.some((w) => w.url === '/chats/compose'), 'not asked again');
  wire.length = 0;
  el = await mountList(new Error('down'));
  assert.equal(el._compose, null);
  el.load();
  assert.ok(wire.some((w) => w.url === '/chats/compose'), 'asked again with the next refresh');
});

await ok('Delete on a mail thread says the mails go to the Trash; a messenger chat\'s as before', async () => {
  const el = await mountList({ channels: [] }, [
    listChat('email:~me@example.org:r1'),
    { ...listChat('signal:~+41000:+41999'), channel: 'signal', account: '+41000', participants: undefined, name: 'Mara' },
  ]);
  el._setScope('unread');
  el._setScope('archived');
  const h = el.shadowRoot.innerHTML;
  const delOf = (id) => {
    const li = h.slice(h.indexOf(`data-id="${id}"`));
    return (/<button type="button" class="delete"[^>]*>Delete<\/button>/.exec(li) || [''])[0];
  };
  const m = delOf('email:~me@example.org:r1');
  assert.ok(m.includes('title="Move this thread&#39;s mails to the Trash of me@example.org, then erase the chat here"'), m);
  assert.ok(m.includes('data-confirm="Move mails to Trash?"'), m);
  const s = delOf('signal:~+41000:+41999');
  assert.ok(s.includes('title="Erase this chat and all its messages from the system"') && !s.includes('data-confirm'), s);
  // The armed button says it.
  const rows = new SwipeRows({}, { onQuick() {} });
  const btn = (confirm) => ({ classList: { add() {}, remove() {} }, dataset: confirm ? { confirm } : {}, textContent: 'Delete' });
  const mailBtn = btn('Move mails to Trash?');
  rows._arm(mailBtn);
  assert.equal(mailBtn.textContent, 'Move mails to Trash?');
  rows._unarm(mailBtn);
  assert.equal(mailBtn.textContent, 'Delete');
  const sigBtn = btn('');
  rows._arm(sigBtn);
  assert.equal(sigBtn.textContent, 'Delete for good?');
});

await ok('a chat that is only a queued mail previews as waiting', async () => {
  const c = listChat('k', { last: { ts: 't', direction: 'out', author: 'user', text: 'Hi <Zoe>', pending: true } });
  assert.equal(previewHtml(c), '<span class="pv-who">You:</span> <span class="pv-pending">Waiting for approval:</span> Hi &lt;Zoe&gt;');
});

// ── The compose sheet ────────────────────────────────────────────────────────
await ok('addresses by shape, split as a header would be', async () => {
  for (const a of ['ann@example.com', 'a.b+tag@sub.example.co', 'jörg@exämple.de', 'x@y.museum']) assert.ok(isAddress(a), a);
  for (const a of ['ann', 'ann@', '@x.org', 'ann@x', 'a b@x.org', 'a@x.o', 'a@@x.org', 'a<b>@x.org', 'a,b@x.org',
                   'a@x..org', `${'a'.repeat(250)}@x.org`, null, 3]) assert.ok(!isAddress(a), String(a));
  const r = splitAddresses('"Doe, Jane" <jane@x.org>, b@y.org; Bob <bob@builder.example>\nlast');
  assert.deepEqual(r.tokens.map((t) => [t.address, t.name]),
                   [['jane@x.org', 'Doe, Jane'], ['b@y.org', ''], ['bob@builder.example', 'Bob'], ['last', '']]);
  assert.equal(r.tail, true);
  assert.equal(splitAddresses('a@x.org,').tail, false);
  assert.deepEqual(splitAddresses(' , ;').tokens, []);
});

function openSheet(offer = { channel: 'email', label: 'E-mail', accounts: [ACCT] }) {
  store.clear();
  assigned.length = 0;
  wire.length = 0;
  let back = 0;
  const el = sheetMod.openComposeSheet(offer, () => { back += 1; });
  return { el, root: el.shadowRoot, back: () => back };
}
const q = (root, s) => root.querySelector(s);
const type = (root, f, value) => { const i = q(root, `[data-input="${f}"]`); i.value = value; root.fire('input', i); return i; };

await ok('the sheet: a labelled modal dialog, focus in To, the page behind inert', async () => {
  const { el, root } = openSheet();
  const h = root.innerHTML;
  assert.ok(h.includes('<form class="sheet" role="dialog" aria-modal="true" aria-labelledby="compose-title" data-form novalidate>'));
  assert.ok(h.includes('<h2 id="compose-title">New e-mail</h2>'));
  assert.ok(h.includes('<p class="from">From <b>me@example.org</b></p>'), 'one mailbox: no choice');
  assert.ok(!h.includes('data-account'));
  for (const f of ['to', 'cc']) {
    assert.ok(h.includes(`<label class="lbl" for="compose-${f}">`), f);
    assert.ok(h.includes(`id="compose-${f}" data-input="${f}"`) && h.includes(`role="combobox"`) && h.includes(`aria-controls="sugg-${f}"`), f);
  }
  assert.ok(h.includes('<label class="lbl" for="compose-subject">Subject</label>'));
  assert.ok(h.includes('<label class="lbl" for="compose-text">Message</label>'));
  assert.equal(globalThis.focused, q(root, '[data-input="to"]'));
  assert.equal(main.inert, true, 'the page behind is inert');
  // Several mailboxes: a choice.
  const two = openSheet({ channel: 'email', label: 'E-mail', accounts: [ACCT, 'work@example.com'] });
  assert.ok(two.root.innerHTML.includes('<select id="compose-from" data-account><option value="me@example.org" selected>'));
  two.el.close();
  assert.equal(main.inert, false);
});

await ok('chips from typing and pasting; what is no address stays, with its error; removal', async () => {
  const { el, root } = openSheet();
  let i = type(root, 'to', 'ann@example.com, bo');
  assert.deepEqual(el._chips.to, [{ address: 'ann@example.com', name: '' }]);
  assert.equal(i.value, 'bo', 'the token still being typed stays');
  assert.ok(q(root, '[data-chips="to"]').innerHTML.includes('aria-label="Remove ann@example.com"'));
  i = type(root, 'to', 'bob;');
  assert.equal(i.value, 'bob');
  assert.equal(q(root, '[data-err="to"]').textContent, '“bob” is not an e-mail address.');
  assert.equal(q(root, '[data-err="to"]').hidden, false);
  assert.equal(i.getAttribute('aria-invalid'), 'true');
  i = type(root, 'to', '"Builder, Bob" <bob@builder.example>, <img src=x>@x.org,');
  assert.deepEqual(el._chips.to.map((r) => r.address), ['ann@example.com', 'bob@builder.example']);
  assert.ok(q(root, '[data-chips="to"]').innerHTML.includes('<span class="chip-txt">Builder, Bob</span>'));
  assert.ok(q(root, '[data-err="to"]').textContent.includes('“<img src=x>@x.org”'), 'shown as text');
  type(root, 'to', '');
  root.fire('focusout', q(root, '[data-input="to"]'));
  assert.equal(q(root, '[data-err="to"]').hidden, true, 'a clean field clears its error');
  // Cc does not copy the To.
  type(root, 'cc', 'ANN@example.com, carla@example.net,');
  assert.deepEqual(el._chips.cc.map((r) => r.address), ['carla@example.net']);
  // Remove: the field takes the focus.
  el._removeChip('to', 0);
  assert.deepEqual(el._chips.to.map((r) => r.address), ['bob@builder.example']);
  assert.equal(globalThis.focused, q(root, '[data-input="to"]'));
  // Enter in a recipient field makes a chip and never submits.
  i = type(root, 'to', 'zoe@x.org');
  const ev = root.fire('keydown', i, { key: 'Enter' });
  assert.ok(ev.defaultPrevented);
  assert.deepEqual(el._chips.to.map((r) => r.address), ['bob@builder.example', 'zoe@x.org']);
  assert.equal(wire.filter((w) => w.method === 'POST').length, 0);
  // Backspace in an empty field takes the last chip back.
  root.fire('keydown', i, { key: 'Backspace' });
  assert.deepEqual(el._chips.to.map((r) => r.address), ['bob@builder.example']);
});

await ok('suggestions from the address book, by keyboard; a stale answer is dropped', async () => {
  const { el, root } = openSheet();
  const i = type(root, 'to', 'an');
  const p = el._suggest('to', 'an');
  const req = take((w) => w.url.startsWith('/contacts?'), 'the address book');
  assert.equal(req.url, '/contacts?q=an');
  req.answer({ contacts: [
    { id: 'p1', name: 'Ann Example', handles: [{ channel: 'email', handle: 'ann@example.com' }, { channel: 'sms', handle: '+41' },
                                               { channel: 'email', handle: 'ann@work.example' }] },
    { id: 'p2', name: '<b>Andi</b>', handles: [{ channel: 'email', handle: 'andi@x.org' }, { channel: 'email', handle: 'not an address' }] },
  ] });
  await p;
  const list = q(root, '[data-sugg="to"]');
  assert.equal(list.hidden, false);
  assert.equal(i.getAttribute('aria-expanded'), 'true');
  assert.deepEqual(el._sugg.items.map((s) => s.address), ['ann@example.com', 'ann@work.example', 'andi@x.org']);
  assert.ok(list.innerHTML.includes('<span class="opt-name">&lt;b&gt;Andi&lt;/b&gt;</span>'));
  assert.ok(list.innerHTML.includes('role="option" id="sugg-to-0"'));
  root.fire('keydown', i, { key: 'ArrowDown' });
  root.fire('keydown', i, { key: 'ArrowDown' });
  assert.equal(i.getAttribute('aria-activedescendant'), 'sugg-to-1');
  root.fire('keydown', i, { key: 'ArrowUp' });
  root.fire('keydown', i, { key: 'Enter' });
  assert.deepEqual(el._chips.to, [{ address: 'ann@example.com', name: 'Ann Example' }]);
  assert.equal(i.value, '');
  assert.equal(list.hidden, true);
  // Escape shuts an open list and only the list.
  type(root, 'to', 'an');
  const p2 = el._suggest('to', 'an');
  take((w) => w.url.startsWith('/contacts?'), 'the address book').answer({ contacts: [{ name: 'A', handles: [{ channel: 'email', handle: 'a2@x.org' }] }] });
  await p2;
  root.fire('keydown', i, { key: 'Escape' });
  assert.equal(el.hasAttribute('hidden'), false, 'the sheet stays');
  assert.equal(q(root, '[data-sugg="to"]').hidden, true);
  // An answer for a fragment the field has moved past is dropped.
  type(root, 'to', 'bo');
  const p3 = el._suggest('to', 'bo');
  type(root, 'to', 'bob');
  take((w) => w.url === '/contacts?q=bo', 'the address book').answer({ contacts: [{ name: 'Bo', handles: [{ channel: 'email', handle: 'bo@x.org' }] }] });
  await p3;
  assert.deepEqual(el._sugg.items, []);
});

await ok('nothing is sent until there is a recipient and a text', async () => {
  const { el, root } = openSheet();
  await el._submit();
  assert.equal(q(root, '[data-err="to"]').textContent, 'Add at least one recipient.');
  assert.equal(q(root, '[data-err="text"]').textContent, 'Write the mail before you send it.');
  assert.equal(q(root, '[data-text]').getAttribute('aria-invalid'), 'true');
  assert.equal(globalThis.focused, q(root, '[data-input="to"]'), 'focus on the first field to fix');
  type(root, 'to', 'not-an-address');
  await el._submit();
  assert.equal(q(root, '[data-err="to"]').textContent, '“not-an-address” is not an e-mail address.');
  assert.equal(wire.length, 0, 'no request');
});

await ok('a send: the request, then the new chat — sent or queued', async () => {
  for (const [status, answer] of [[200, { id: 'email:~me@example.org:new1@me.example.org', message: { id: 'new1@me.example.org' } }],
                                  [202, { id: 'email:~me@example.org:new2@me.example.org', pending: pend('q7') }]]) {
    const { el, root } = openSheet({ channel: 'email', label: 'E-mail', accounts: [ACCT, 'work@example.com'] });
    type(root, 'to', 'Ann <ann@example.com>, bob@builder.example');
    type(root, 'cc', 'carla@example.net');
    const subj = q(root, '[data-subject]'); subj.value = 'Numbers'; root.fire('input', subj);
    const text = q(root, '[data-text]'); text.value = 'Hi both,\n\nthe numbers.'; root.fire('input', text);
    const acct = q(root, '[data-account]'); acct.value = 'work@example.com'; root.fire('change', acct);
    // Ctrl+Enter in the text sends; Enter there is a line break.
    assert.ok(!root.fire('keydown', text, { key: 'Enter' }).defaultPrevented);
    root.fire('keydown', text, { key: 'Enter', ctrlKey: true });
    await settle();
    const req = take((w) => w.method === 'POST', 'the compose');
    assert.equal(req.url, '/chats/compose');
    assert.deepEqual(req.body, { channel: 'email', to: ['ann@example.com', 'bob@builder.example'], cc: ['carla@example.net'],
                                 subject: 'Numbers', text: 'Hi both,\n\nthe numbers.', account: 'work@example.com' });
    assert.equal(q(root, '[data-send]').textContent, 'Sending …');
    assert.equal(q(root, '[data-send]').disabled, true);
    el._submit();
    assert.equal(wire.filter((w) => w.method === 'POST').length, 0, 'one press, one request');
    req.answer(answer, status);
    await settle();
    await settle();
    assert.deepEqual(assigned, [`/chat.html?id=${encodeURIComponent(answer.id)}`]);
    assert.equal(store.get('retinue-draft:compose:email'), undefined, 'the kept mail is dropped');
  }
});

await ok('a refusal is shown in place, everything still there', async () => {
  const cases = [
    [{ error: 'The address "x@y.zz" does not parse.' }, 400, 'Not sent: The address "x@y.zz" does not parse.'],
    [{}, 403, 'Not sent: the server refused the request.'],
    [{}, 503, 'Not sent (the server answered 503). Try again in a moment.'],
    [{ message: {} }, 200, 'The server did not say where the new chat is — look for it in the chat list.'],
    [null, 0, "Couldn't reach the server — check the connection and try again."],
  ];
  for (const [doc, status, said] of cases) {
    const { el, root } = openSheet();
    type(root, 'to', 'ann@example.com,');
    const text = q(root, '[data-text]'); text.value = 'Hello'; root.fire('input', text);
    const p = el._submit();
    const req = take((w) => w.method === 'POST', 'the compose');
    if (status) req.answer(doc, status); else req.fail();
    await p;
    const line = q(root, '[data-err="form"]');
    assert.equal(line.textContent, said);
    assert.equal(line.hidden, false);
    assert.ok(root.innerHTML.includes('role="alert"'));
    assert.equal(q(root, '[data-send]').disabled, false);
    assert.deepEqual(el._chips.to.map((r) => r.address), ['ann@example.com']);
    assert.equal(el._text, 'Hello');
    assert.deepEqual(assigned, []);
  }
});

await ok('Escape closes, the focus goes back, and the mail waits for the next opening', async () => {
  const { el, root, back } = openSheet();
  type(root, 'to', 'ann@example.com,');
  const subj = q(root, '[data-subject]'); subj.value = 'Later'; root.fire('input', subj);
  // Enter in the subject goes on to the text.
  const ev = root.fire('keydown', subj, { key: 'Enter' });
  assert.ok(ev.defaultPrevented);
  assert.equal(globalThis.focused, q(root, '[data-text]'));
  root.fire('keydown', subj, { key: 'Escape' });
  assert.equal(el.hasAttribute('hidden'), true);
  assert.equal(back(), 1);
  assert.equal(main.inert, false);
  // Opened again: what it held, said so.
  let back2 = 0;
  sheetMod.openComposeSheet({ channel: 'email', label: 'E-mail', accounts: [ACCT] }, () => { back2 += 1; });
  assert.deepEqual(el._chips.to.map((r) => r.address), ['ann@example.com']);
  assert.equal(el._subject, 'Later');
  assert.ok(el.shadowRoot.innerHTML.includes('The mail you started earlier is back.'));
  assert.ok(el.shadowRoot.innerHTML.includes('value="Later"'));
  // Discard empties it for good.
  el._discard();
  assert.equal(store.get('retinue-draft:compose:email'), undefined);
  assert.deepEqual(el._chips.to, []);
  // Tab wraps inside the dialog.
  const first = new El('first'); const last = new El('last');
  el.shadowRoot.querySelectorAll = () => [first, last];
  el.shadowRoot.activeElement = last;
  const tab = el.shadowRoot.fire('keydown', last, { key: 'Tab' });
  assert.ok(tab.defaultPrevented && globalThis.focused === first);
  el.shadowRoot.activeElement = first;
  el.shadowRoot.fire('keydown', first, { key: 'Tab', shiftKey: true });
  assert.equal(globalThis.focused, last);
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def static_checks() -> list[str]:
    """What the sources must hold to, whether or not Node is here."""
    errors = []
    with open(os.path.join(COMPONENTS, "chat-page.js"), encoding="utf-8") as fh:
        src = fh.read()
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("//"))
    # The approval link is built by DOM calls; no markup string carries one.
    if re.search(r"<a [^`]*pend-link", code):
        errors.append("chat-page.js builds the approval link from markup")
    if "createElement('a')" not in code:
        errors.append("chat-page.js no longer builds the approval link with createElement")
    render = re.search(r"\n  render\(\) \{(.*?)\n  \}\n", src, re.S)
    if not render or "this._mountApprovals()" not in render.group(1):
        errors.append("render() no longer mounts the pending bubbles' approval links")
    with open(os.path.join(WEBAPP, "sw.js"), encoding="utf-8") as fh:
        if "'/components/compose-sheet.js'" not in fh.read():
            errors.append("compose-sheet.js is not in sw.js SHELL_ASSETS")
    return errors


def main() -> int:
    errors = static_checks()
    if errors:
        for e in errors:
            print("FAIL: " + e)
        return 1
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the mail writing test needs it")
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
            print("FAIL: mail writing test")
            return 1
    print("PASS: mail writing test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
