#!/usr/bin/env python3
"""Writing mail in the dashboard (webapp/components/chat-page.js, chats.js,
compose-sheet.js and swipe-rows.js; docs/mail-chats.md, "Writing").

The elements are driven under Node with a scripted DOM and a scripted fetch,
and pin:

  * whom a reply reaches is the server's word (GET /chats/<id>/reply-preview)
    and never worked out by the page: the document read whole or not at all
    (replyPreview), names shown only beside their address and dropped where
    they read as one (partyName), the address book's name before the mail's;
    the To/Cc line, the send button's label and the Reply all / Sender only
    choice drawn from that answer alone — a Reply-To, the user's own mail
    being the latest, an alias; a To that is not where the latest mail came
    from says why; asked on load, after every poll that changed the thread
    (a mail added, changed, removed or gone, a pending send, the people) and
    not otherwise; an overtaken answer dropped; the pair put on the page and
    taken off it, not only checked;
  * no answer, no reply: unanswered, `unavailable` (a server without the
    endpoint, a document that is not one), away, or the gateway's own "no
    reply here" (a chat that holds only a queued mail) — the line says so,
    the button is off, nothing is posted and the words stay;
  * the press: the mode read at the press and reset there, the recipients
    asked for once more, and nothing posted unless that answer is there, not
    older than the thread on screen and the line the user was looking at;
    `reply` and one `request_key` in the body; messenger composers and sends
    as before;
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
  * what became of a send that was let go: `sending` and `failed` entries
    in words, the reason as text, "may have gone out" where nobody knows,
    the text back into the composer (after what it holds) and Dismiss (one
    request, a refusal shown, a lagging poll not drawing it again), the
    recipients the mail server refused named on the bubble and on the mail;
  * the approval link: built by DOM calls, only from a path that is exactly
    /sends/<slug>/<id> — anything else plain text;
  * a mail that may have gone out is never called "not sent": 202
    `{unconfirmed}` keeps the bubble ("Sending — not confirmed yet") and
    never restores the words; it gives way to the mail (by Message-ID, never
    by words) or to the server's pending entry (by Message-ID or
    request_key, the focus going with it); it is kept on the device — in a
    record of its own, which costs the composer's kept words nothing — and
    comes back after a reload until it is accounted for or dismissed, with
    its words offered back and never in silence — to the clipboard where
    the chat cannot be written to; two tabs on one chat each keep the
    other's mails in the record; a time no date can be made of is no
    record, and a record that cannot be read does not keep the chat from
    opening; Check again posts the same
    body under the same key, but only after the recipients were asked for
    and still are whom the press's line named; no answer, a 5xx that does
    not say `"sent": false` or a 200 that is no document put the words back
    with the key they went under, so the same press again is the same mail
    — also across a reload and a page left mid-send;
  * a send that certainly failed ends its press: any 4xx and any answer
    with `"sent": false` is "Not sent" and the words stay; the next press
    mints a new key where that word is the mail gateway's (`"by":
    "gateway"`, or no `"by"` on a first press), while the dashboard's own
    keeps the key, known to have sent nothing; a press made again under a
    key whose first outcome is unknown, refused by anyone but the mail
    gateway (the dashboard, a proxy, an older server), keeps its key and
    says the server could not be asked — on the composer, Check again and
    the compose sheet alike;
  * the key's life: one per press, dropped as soon as the box holds other
    words (even for a moment), and when the press turns up in the thread —
    a mail like it, or the server's entry under its key (the words then
    leave the box; an entry that failed for certain leaves them); a poll
    draws no second bubble for the press in flight; an entry marked
    `dismissed` is over and never drawn;
  * the address book's name for a recipient is `contact`, the person's id
    `contact_id`: a lowercase one-word name is shown as filed;
  * ＋ on the Chats page: present only when GET /chats/compose offers
    something, its menu, the sheet it opens, the mailboxes on offer asked
    for again as it is opened; the delete wording of a mail
    thread (the mails move to the mailbox's Trash) and of a messenger chat;
    a pending preview in the list, by its state;
  * the compose sheet: address shape and splitting, chips from typing and
    pasting, invalid input left in place with its error, removal, the
    recipient cap (what it kept out named and left in the field, cleared
    from the keyboard), address book suggestions with the keyboard (stale
    answers dropped, none under a field that was left), an input method's
    Enter (also WebKit's, keyCode 229), validation before any request, the
    request with its `request_key`, navigation to the new chat on 200 and on
    202, one press one mail (closing and opening the sheet does not arm Send
    again; a failure after closing brings the sheet back), errors in place
    and never "not sent" where the outcome is open, the key kept with the
    mail, Escape and the focus given back, the page behind inert, the unsent
    mail kept per mailbox and never moved to another one unseen — nor
    forgotten once another mailbox is written from: every opening names the
    mailboxes that hold one (and renews them, so none is dropped for its
    age), a mail whose mailbox can send again comes back, and one whose
    mailbox is not offered is shown, to be copied into the mail in the form
    or discarded; a kept press is asked about again under its key only
    within twelve hours;
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
const { replyPreview, partyName, approvalPath, QUICK_PATTERNS } = page;
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
const PV_URL = (id = ID) => `/chats/${encodeURIComponent(id)}/reply-preview`;
const SEND_URL = `/chats/${encodeURIComponent(ID)}/send`;
const KEY_RE = /^[A-Za-z0-9_-]{16,64}$/;
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
  reply_to: [], trim: 'deterministic', removed: false, removed_at: null, attachments: [], ...extra,
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

// The server's word on whom a reply reaches (GET /chats/<id>/reply-preview),
// as the gateway would address it for BASE: Ann wrote last, to the user and
// Bob, copying Carla.
const ANN = { address: 'ann@example.com', name: 'Ann E.', contact: 'Ann Example' };
const BOB = { address: 'bob@builder.example', name: 'Bob Builder', contact: null };
const CARLA = { address: 'carla@example.net', name: null, contact: null };
const PV = (extra = {}) => ({
  all: { to: [ANN], cc: [BOB, CARLA] }, sender: { to: [ANN], cc: [] }, choice: true,
  subject: 'Re: Quarterly numbers', ...extra,
});
const SOLO = () => PV({ all: { to: [ANN], cc: [] }, choice: false });

// The chat page with its thread: render() rebuilds the thread from
// _chatThreadHtml and mounts the approval links, as the real one does.
// `realKeep`: the page keeps the composer's words on the device as the real
// one does, also while it loads (otherwise only where a test turns it on).
let realKeep = false;
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
  if (!realKeep) el._keepLocal = () => {};
}
// One request for the reply preview, answered: a document (200), [document,
// status], or 'offline'.
function give(req, preview) {
  if (preview === 'offline') req.fail();
  else if (Array.isArray(preview)) req.answer(preview[0], preview[1]);
  else req.answer(clone(preview));
}
function answerPreview(el, preview, what = 'the reply preview') {
  const req = take((w) => w.url === PV_URL(el._id) && w.method === 'GET', what);
  give(req, preview);
  return req;
}
// The page asks again and is told `preview`: what it holds from here on.
async function tell(el, preview) {
  const p = el._fetchPreview();
  answerPreview(el, preview);
  await p;
  el._pv = preview;
}
const asked = (el) => wire.filter((w) => w.url === PV_URL(el._id)).length;
// `preview`: what the server answers the page's first question with (and,
// until a test says otherwise, every later one); null leaves it unanswered.
async function open(chat = {}, messages = BASE, pending = [], preview = PV(), keep = false) {
  wire.length = 0;
  if (!keep) store.clear();
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
  el._pv = preview;
  const s = summary(chat);
  if (s.channel === 'email' && s.can_send !== false) {
    assert.equal(asked(el), 1, 'the page asks where a reply would go as it opens');
    if (preview !== null) {
      answerPreview(el, preview);
      await settle();
    }
  } else {
    assert.equal(asked(el), 0, 'no reply preview where there is no mail composer');
  }
  return el;
}
// A poll, and — where it changed the thread — the question it makes the page
// ask again, answered with `preview` (by default as before). → whether the
// page asked.
async function poll(el, messages, pending, chat = {}, preview = undefined) {
  const p = el._poll();
  take((w) => w.url === el._msgUrl && w.method === 'GET', 'the poll')
    .answer({ chat: summary(chat), messages: clone(messages), ...(pending === undefined ? {} : { pending: clone(pending) }) });
  await p;
  const again = asked(el) > 0;
  if (preview !== undefined) el._pv = preview;
  if (again && el._pv !== null) {
    answerPreview(el, el._pv);
    await settle();
  }
  return again;
}
// What the page asks on its own after an answer (the recipients again, the
// chat's pending sends): answered as before, or dropped.
async function quiet(el) {
  await settle();
  for (const w of wire.filter((x) => x.url === PV_URL(el._id))) {
    wire.splice(wire.indexOf(w), 1);
    if (el._pv !== null) give(w, el._pv);
  }
  for (const w of wire.filter((x) => x.url === el._msgUrl && x.method === 'GET')) wire.splice(wire.indexOf(w), 1);
  await settle();
}
// A press of Send. On a mail chat the recipients are asked for once more
// before anything is posted — answered `at` (by default what the page was
// told last) — then the send leaves and is answered.
async function send(el, text, answer, status = 200, at = undefined) {
  const p = el._sendChat(text);
  await settle();
  if (el._chat.channel === 'email') {
    answerPreview(el, at === undefined ? el._pv : at, 'the preview at the press');
    await settle();
  }
  const req = take((w) => w.method === 'POST' && w.url.endsWith('/send'), 'the send');
  if (answer === 'offline') req.fail();
  else req.answer(answer, status);
  await p;
  await quiet(el);
  return req;
}
// A press that must post nothing.
async function refusedPress(el, text, at = undefined) {
  const before = wire.filter((w) => w.method === 'POST').length;
  const p = el._sendChat(text);
  await settle();
  if (at !== undefined) {
    answerPreview(el, at, 'the preview at the press');
    await settle();
  }
  await p;
  assert.equal(wire.filter((w) => w.method === 'POST').length, before, 'nothing was posted');
  await quiet(el);
}
const link = (el, rid) => {
  const n = el._thread.pnode(rid);
  assert.ok(n, `pending bubble ${rid}`);
  return n._slot;
};
const sentMsg = (id, text, extra = {}) => ({ id, chat: ID, direction: 'out', author: 'user', text, sender: ACCT,
                                            ts: '2026-11-20T11:00:00Z', ...extra });

// ── Whom a reply reaches: the server says ────────────────────────────────────
await ok('the page has no addressing of its own', async () => {
  assert.equal(page.replyRecipients, undefined, 'the client-side derivation is gone');
  assert.equal(typeof replyPreview, 'function');
});

await ok('the reply preview is read whole or not at all', async () => {
  const p = replyPreview(PV());
  assert.deepEqual(p.all, { to: [{ address: 'ann@example.com', name: 'Ann Example' }],
                            cc: [{ address: 'bob@builder.example', name: 'Bob Builder' },
                                 { address: 'carla@example.net', name: '' }] }, 'the address book\'s name before the mail\'s');
  assert.deepEqual(p.sender, { to: [{ address: 'ann@example.com', name: 'Ann Example' }], cc: [] });
  assert.equal(p.choice, true);
  assert.equal(p.subject, 'Re: Quarterly numbers');
  // The address book's word as {name}; a person's id is no name.
  const named = (contact, name = 'Mail Name') => replyPreview(PV({ sender: { to: [{ address: 'a@x.org', name, contact }], cc: [] } })).sender.to[0].name;
  assert.equal(named({ name: 'Book Name' }), 'Book Name');
  assert.equal(named('urn:retinue:person:ann-example'), 'Mail Name');
  assert.equal(named('ann-example'), 'Mail Name');
  assert.equal(named(null), 'Mail Name');
  assert.equal(named(7), 'Mail Name');
  // The gateway's answer names the person's id beside the name (`contact_id`),
  // and the name is then shown as it is filed — also a lowercase one-word
  // one, which alone would read as an id — before whatever a mail called
  // its own sender.
  const filed = (contact, contact_id, name = 'Totally Your Bank') => replyPreview(PV({
    sender: { to: [{ address: 'mum@example.org', name, contact, contact_id }], cc: [] } })).sender.to[0].name;
  assert.equal(filed('mum', 'private:contacts/mum'), 'mum');
  assert.equal(filed('anna', 'private:contacts/anna'), 'anna');
  assert.equal(filed('acme-gmbh', 'work:contacts/acme', 'Invoice Dept'), 'acme-gmbh');
  assert.equal(filed('Helga Schmidt', 'private:contacts/mum'), 'Helga Schmidt');
  assert.equal(filed({ name: 'mum' }, 'private:contacts/mum'), 'mum');
  // Nobody's address has no address-book name, whatever stands beside it;
  // and an id is never its own name.
  assert.equal(filed('mum', null), 'Totally Your Bank');
  assert.equal(filed('mum', ''), 'Totally Your Bank');
  assert.equal(filed('mum', 7), 'Totally Your Bank');
  assert.equal(filed('private:contacts/mum', 'private:contacts/mum'), 'Totally Your Bank');
  assert.equal(filed(null, 'private:contacts/mum'), 'Totally Your Bank');
  // A name that reads as an address is dropped wherever it came from.
  assert.equal(filed('ceo@company.com', 'private:contacts/x', null), '');
  // `choice` only on the server's plain word; a Cc left out is none.
  assert.equal(replyPreview(PV({ choice: 'yes' })).choice, false);
  assert.deepEqual(replyPreview(PV({ sender: { to: [ANN] } })).sender.cc, []);
  // One recipient the page could not show, and nothing is shown at all.
  for (const bad of [null, 'x', [], {}, PV({ all: null }), PV({ sender: undefined }), PV({ all: { to: 'ann@example.com', cc: [] } }),
                     PV({ all: { to: [ANN], cc: 'bob' } }), PV({ all: { to: [ANN, { name: 'No Address' }], cc: [] } }),
                     PV({ all: { to: [ANN], cc: [BOB, 'carla@example.net'] } }), PV({ sender: { to: [{ address: '  ' }], cc: [] } }),
                     PV({ all: { to: [{ address: `${'a'.repeat(400)}@x.org` }], cc: [] } }),
                     // An address that would not read on screen as what it is.
                     PV({ all: { to: [{ address: `ann@example.com${String.fromCharCode(0x202e)}gro.live@` }], cc: [] } }),
                     PV({ all: { to: [{ address: `ann@exam${String.fromCharCode(0x200b)}ple.com` }], cc: [] } }),
                     PV({ all: { to: [ANN], cc: [{ address: 'bob@builder.example, eve@evil.example' }] } })]) {
    assert.equal(replyPreview(bad), null, JSON.stringify(bad).slice(0, 80));
  }
  // The same address twice in a row reads the same both times.
  assert.ok(replyPreview(PV()) && replyPreview(PV()));
});

await ok('a name is shown only where it cannot be read as an address', async () => {
  assert.equal(partyName('Ann Example', 'ann@example.com'), 'Ann Example');
  // A display name a mail supplied that is itself an address, or dresses up
  // as one, is dropped: the address stands alone.
  const bidi = String.fromCharCode(0x202e);
  const fullAt = String.fromCharCode(0xff20);
  for (const n of ['ceo@company.com', 'Ann <ann@good.org>', `ceo${fullAt}company.com`, 'x@evil.example', 'X@Evil.Example',
                   '<Ann>', '', '   ', null, 42]) {
    assert.equal(partyName(n, 'x@evil.example'), '', String(n));
  }
  assert.equal(partyName(`Ann${bidi}  Example\n`, 'a@x.org'), 'Ann Example', 'control and direction characters go');
  assert.equal(partyName('N'.repeat(200), 'a@x.org').length, 60, 'a long name does not push the address away');
  // In the line: the spoofed name is gone, the real address is what is read.
  const el = await open({}, BASE, [], PV({ all: { to: [{ address: 'x@evil.example', name: 'ceo@company.com', contact: null }], cc: [] },
                                           sender: { to: [{ address: 'x@evil.example', name: 'ceo@company.com', contact: null }], cc: [] },
                                           choice: false }));
  const h = el._chatComposerHtml();
  assert.ok(h.includes('To <span class="rcp"><b>x@evil.example</b>'), h);
  assert.ok(!h.includes('ceo@company.com'), 'the name that reads as another address is nowhere');
});

// ── The composer ─────────────────────────────────────────────────────────────
const LINE_ALL = '<span aria-hidden="true">&#10148;</span> To <span class="rcp"><b>Ann Example</b> ' +
  '<span class="addr">&lt;ann@example.com&gt;</span></span> &middot; Cc <span class="rcp"><b>Bob Builder</b> ' +
  '<span class="addr">&lt;bob@builder.example&gt;</span></span>, <span class="rcp"><b>carla@example.net</b></span></div>';
const LINE_SENDER = '<span aria-hidden="true">&#10148;</span> To <span class="rcp"><b>Ann Example</b> ' +
  '<span class="addr">&lt;ann@example.com&gt;</span></span></div>';

await ok('a mail composer: the server\'s To/Cc with every address, the choice, mail wording', async () => {
  const el = await open();
  const h = el._chatComposerHtml();
  assert.ok(h.includes('<div class="to-line mail" data-to-line title="Your reply “Re: Quarterly numbers” goes to ' +
    'Ann Example &lt;ann@example.com&gt;, with a copy to Bob Builder &lt;bob@builder.example&gt;, carla@example.net ' +
    '— by e-mail, not to Ara.">' + LINE_ALL), h);
  assert.ok(h.includes('role="radiogroup" aria-label="Who the reply goes to"'));
  assert.ok(h.includes('value="all" data-reply-mode checked> Reply all'), h);
  assert.ok(h.includes('value="sender" data-reply-mode> Sender only'), h);
  assert.ok(h.includes('placeholder="Write your reply …"'));
  assert.ok(h.includes('aria-label="Your reply — Enter starts a new line, Ctrl+Enter or ⌘+Enter sends"'));
  assert.ok(h.includes('enterkeyhint="enter"'));
  assert.ok(h.includes('aria-label="Send the reply to Ann Example &lt;ann@example.com&gt;, copy to 2 more (Ctrl+Enter or ⌘+Enter)">'), h);
  assert.ok(!h.includes('aria-disabled'), 'the send button is live');
  el._setReplyMode('sender');
  const s = el._chatComposerHtml();
  assert.ok(s.includes(LINE_SENDER), 'the sender\'s side of the answer');
  assert.ok(s.includes('value="sender" data-reply-mode checked'));
  assert.ok(s.includes('aria-label="Send the reply to Ann Example &lt;ann@example.com&gt; (Ctrl+Enter or ⌘+Enter)"'), s);
  // No choice where the server says the two are the same — whatever the
  // thread looks like to the page (a group, a Cc on the mail).
  const solo = await open({}, BASE, [], SOLO());
  assert.ok(!solo._chatComposerHtml().includes('data-reply-mode'));
  assert.ok(solo._chatComposerHtml().includes(LINE_SENDER));
  // And one in a thread the summary calls a 1:1, when the server says so.
  const one = await open({ group: false, participants: [PEOPLE[0]] });
  assert.ok(one._chatComposerHtml().includes('data-reply-mode'));
});

await ok('the line is the server\'s answer, never the thread\'s arithmetic', async () => {
  // Ann wrote to the user, Bob and Carla; the user answered Ann alone. The
  // gateway addresses a follow-up to that mail's recipients again — Ann —
  // and that is what the line says, with no choice to make.
  const own = [BASE[0], mail('own', 'out', '2026-11-20T10:00:00Z', { author: 'user', sender: ACCT, recipients: ['ann@example.com'], cc: [] })];
  let el = await open({}, own, [], SOLO());
  let h = el._chatComposerHtml();
  assert.ok(h.includes(LINE_SENDER) && !h.includes('Bob Builder') && !h.includes('data-reply-mode'), h);
  // Ann wrote to the user alone; the user's own later mail (from a mail
  // client) added Dave and copied Erin: the next reply reaches them too.
  const DAVE = { address: 'dave@w.org', name: null, contact: null };
  const ERIN = { address: 'erin@v.org', name: 'Erin', contact: null };
  const wide = [mail('a', 'in', '2026-11-20T08:00:00Z', { recipients: [ACCT], cc: [] }),
                mail('b', 'out', '2026-11-20T09:00:00Z', { author: 'device', sender: ACCT, recipients: ['ann@example.com', 'dave@w.org'], cc: ['erin@v.org'] })];
  el = await open({ group: false, participants: [PEOPLE[0]] }, wide, [],
                  PV({ all: { to: [ANN, DAVE], cc: [ERIN] }, sender: { to: [ANN, DAVE], cc: [] } }));
  h = el._chatComposerHtml();
  assert.ok(h.includes('<b>dave@w.org</b>') && h.includes('Cc <span class="rcp"><b>Erin</b> <span class="addr">&lt;erin@v.org&gt;</span>'), h);
  // An alias, a participant list that is capped, an address the page never
  // heard of: nothing is filtered here — what the server names is shown.
  el = await open({ participants: [], participants_more: 4 }, BASE, [],
                  PV({ all: { to: [ANN], cc: [{ address: 'me-alias@example.org', name: null, contact: null }] } }));
  assert.ok(el._chatComposerHtml().includes('<b>me-alias@example.org</b>'));
});

await ok('a reply that does not go where the mail came from says so', async () => {
  // A Reply-To: the line names the address the reply goes to, and why.
  const LIST = { address: 'all-staff@lists.example', name: 'Ann Example', contact: null };
  const EVE = { address: 'x@evil.example', name: null, contact: null };
  const msgs = [mail('r', 'in', '2026-11-20T08:00:00Z', { recipients: [ACCT], cc: [], reply_to: ['All-Staff@lists.example', 'x@evil.example'] })];
  let el = await open({}, msgs, [], PV({ all: { to: [LIST, EVE], cc: [] }, sender: { to: [LIST, EVE], cc: [] }, choice: false }));
  let h = el._chatComposerHtml();
  assert.ok(h.includes('<b>Ann Example</b> <span class="addr">&lt;all-staff@lists.example&gt;</span> ' +
    '<span class="rcp-why">(the Reply-To of the mail from ann@example.com)</span>'), h);
  assert.ok(h.includes('<b>x@evil.example</b> <span class="rcp-why">(the Reply-To of the mail from ann@example.com)</span>'), h);
  assert.ok(h.includes('aria-label="Send the reply to Ann Example &lt;all-staff@lists.example&gt;, x@evil.example (Ctrl'), h);
  // Not the sender, and no Reply-To the page was told of (an older record):
  // still said, in plainer words.
  el = await open({}, [mail('o', 'in', '2026-11-20T08:00:00Z', { reply_to: undefined })], [], PV({ all: { to: [EVE], cc: [] }, sender: { to: [EVE], cc: [] }, choice: false }));
  h = el._chatComposerHtml();
  assert.ok(h.includes('<span class="rcp-why">(not the address the latest mail came from, ann@example.com)</span>'), h);
  // The sender's own address needs no note; nor does a follow-up to the
  // user's own mail, whose recipients are whom they chose.
  el = await open();
  assert.ok(!el._chatComposerHtml().includes('rcp-why'));
  el = await open({}, [...BASE, mail('mine', 'out', '2026-11-20T10:00:00Z', { author: 'user', sender: ACCT, recipients: ['bob@builder.example'], cc: [] })], [],
                  PV({ all: { to: [BOB], cc: [] }, sender: { to: [BOB], cc: [] }, choice: false }));
  assert.ok(!el._chatComposerHtml().includes('rcp-why'));
  // A hostile sender address is text.
  el = await open({}, [mail('h', 'in', '2026-11-20T08:00:00Z', { sender: '"><img src=x>@evil.example' })], [], SOLO());
  h = el._chatComposerHtml();
  assert.ok(h.includes('&quot;&gt;&lt;img src=x&gt;@evil.example') && !h.includes('<img'), h);
});

await ok('no answer, no reply: unknown recipients are never guessed', async () => {
  // Not answered yet: the line says so, the button is off, nothing is sent.
  let el = await open({}, BASE, [], null);
  let h = el._chatComposerHtml();
  assert.ok(h.includes('<div class="to-line mail wait" data-to-line role="status"><span aria-hidden="true">&#10148;</span> ' +
    'Checking where a reply would go …</div>'), h);
  assert.ok(h.includes('aria-label="Send the reply — not possible until the recipients are known" aria-disabled="true"'), h);
  assert.ok(!h.includes('data-reply-mode') && !h.includes('Ann Example'), 'nobody is named');
  let restored = '';
  el._restoreComposer = (t) => { restored = t; };
  wire.length = 0;
  await refusedPress(el, 'hello');
  assert.equal(restored, 'hello', 'the words are back');
  assert.equal(el._notes.at(-1), 'Checking where a reply would go — try again in a moment.');
  // A server without the endpoint: said, and not asked again on every poll.
  for (const status of [404, 405, 501]) {
    el = await open({}, BASE, [], [{ error: 'not found' }, status]);
    h = el._chatComposerHtml();
    assert.ok(h.includes('<div class="to-line mail off" data-to-line role="status">'), h);
    assert.ok(h.includes('Recipients unavailable: this server does not say where a reply would go, so none is sent from here.'), h);
    assert.ok(h.includes('aria-disabled="true"'));
    assert.equal(await poll(el, BASE, []), false, 'nothing changed, nothing asked');
    wire.length = 0;
    await refusedPress(el, 'x', [{}, status]);
    assert.ok(el._notes.at(-1).startsWith('Recipients unavailable:'));
  }
  // An answer that is not the document counts the same.
  el = await open({}, BASE, [], { all: { to: [{ name: 'Ann' }], cc: [] }, sender: { to: [], cc: [] } });
  assert.ok(el._chatComposerHtml().includes('Recipients unavailable: this server does not say'));
  // The gateway away: said, and asked again with the next poll.
  for (const down of [[{ error: 'gateway unreachable' }, 502], 'offline']) {
    el = await open({}, BASE, [], down);
    assert.ok(el._chatComposerHtml().includes('Recipients unavailable right now: a reply is sent only once the server has said where it goes.'));
    const p = el._poll();
    take((w) => w.url === MSG_URL, 'the poll').answer({ chat: summary(), messages: clone(BASE), pending: [] });
    await p;
    assert.equal(asked(el), 1, 'asked again though the thread did not move');
    answerPreview(el, PV());
    await settle();
    assert.ok(el._chatComposerHtml().includes(LINE_ALL));
  }
});

await ok('a thread the gateway takes no reply in says so instead of offering one', async () => {
  // A chat that holds only a queued mail: the server's sentence, and no send.
  const el = await open({}, [], [pend('q1', { to: ['zoe@x.org'], cc: [] })],
                        [{ error: 'This thread has no mail to reply to yet <its first mail waits for approval>.' }, 409]);
  assert.deepEqual(el._thread.layout(), ['P:q1'], 'no "No messages." line');
  const h = el._chatComposerHtml();
  assert.ok(h.includes('<div class="to-line mail off" data-to-line role="status"><span aria-hidden="true">&#10148;</span> ' +
    'No reply can be sent from here: This thread has no mail to reply to yet &lt;its first mail waits for approval&gt;.</div>'), h);
  assert.ok(h.includes('aria-disabled="true"') && !h.includes('zoe@x.org'), 'the queued mail\'s recipients are not offered as a reply\'s');
  assert.ok(h.includes('<textarea'), 'the words can still be written');
  let restored = '';
  el._restoreComposer = (t) => { restored = t; };
  wire.length = 0;
  await refusedPress(el, 'a follow-up');
  assert.equal(restored, 'a follow-up');
  assert.ok(el._notes.at(-1).startsWith('No reply can be sent from here: This thread has no mail'));
  // The mail is approved and arrives: the poll asks again and the reply opens.
  const sent = mail('first@me.example.org', 'out', '2026-11-20T10:05:00Z', { author: 'user', sender: ACCT, recipients: ['zoe@x.org'], cc: [] });
  const ZOE = { address: 'zoe@x.org', name: null, contact: null };
  assert.equal(await poll(el, [sent], [], {}, PV({ all: { to: [ZOE], cc: [] }, sender: { to: [ZOE], cc: [] }, choice: false })), true);
  assert.ok(el._chatComposerHtml().includes('To <span class="rcp"><b>zoe@x.org</b></span></div>'));
  assert.ok(!el._chatComposerHtml().includes('aria-disabled'));
  // Rejected instead: the thread is empty again.
  const gone = await open({}, [], [pend('q1')], [{ error: 'No mail here.' }, 409]);
  await poll(gone, [], []);
  assert.deepEqual(gone._thread.layout(), []);
  const none = await open({}, [], [], [{ error: 'No mail here.' }, 400]);
  assert.deepEqual(none._thread.layout(), ['EMPTY']);
  // A 200 that names nobody is the same "no reply".
  const empty = await open({}, BASE, [], PV({ all: { to: [], cc: [] }, sender: { to: [], cc: [] }, choice: false }));
  assert.ok(empty._chatComposerHtml().includes('No reply can be sent from here: this thread has no mail to reply to.'));
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
  for (const no of ['data-reply-mode', 'enterkeyhint', 'Write your reply', 'aria-disabled']) assert.ok(!h.includes(no), no);
  // And a send carries no reply choice, no key, and asks nobody first.
  const req = await send(el, 'yo', { id: 'b', chat: SID, direction: 'out', author: 'user', text: 'yo', ts: '2026-11-20T08:01:00Z' });
  assert.deepEqual(req.body, { text: 'yo' });
  assert.ok(!wire.some((w) => w.url.includes('reply-preview')));
  // A failed messenger send reads as before.
  await send(el, 'again', { error: 'down' }, 502);
  assert.equal(el._notes.at(-1), "Couldn't send &mdash; check the connection and try again.");
});

// A composer's own nodes, enough for _refreshToLine: the line, the radio
// pair after it (or none) and the send button, each replaced as the page
// replaces them.
function composerDom(el) {
  const realCreate = document.createElement;
  const dom = { line: null, pair: null, send: new El('send') };
  const node = (html) => {
    const n = { html, changes: [] };
    n.radios = [...html.matchAll(/value="(all|sender)" data-reply-mode/g)]
      .map((m) => ({ value: m[1], checked: false, addEventListener: (t, f) => n.changes.push(f) }));
    n.replaceWith = (x) => { if (dom.line === n) dom.line = x; };
    n.insertAdjacentElement = (where, x) => { assert.equal(where, 'afterend'); dom.pair = x; };
    n.remove = () => { if (dom.pair === n) dom.pair = null; };
    n.contains = () => false;
    n.querySelectorAll = (s) => (s === '[data-reply-mode]' ? n.radios : []);
    return n;
  };
  dom.line = node(el._toLineHtml());
  if (el._replyModeHtml()) dom.pair = node(el._replyModeHtml());
  // Only the composer's own pieces are made here; a bubble redrawn by the
  // same poll is the thread's.
  document.createElement = (tag) => {
    if (tag !== 'template') return realCreate(tag);
    const real = realCreate('template');
    return { set innerHTML(h) {
      if (/^<div class="(to-line|reply-mode)/.test(h)) this.content = { firstElementChild: node(h) };
      else { real.innerHTML = h; this.content = real.content; }
    } };
  };
  const thread = el._thread;
  el.shadowRoot = {
    activeElement: null,
    querySelector: (s) => {
      if (s === '[data-to-line]') return dom.line;
      if (s === '.reply-mode') return dom.pair;
      if (s === '[data-composer="chat"] .send') return dom.send;
      if (s === '[data-chat-thread]') return thread;
      const m = /^\[data-chat-thread\] \[data-mid="(.*)"\]$/.exec(s);
      return m && thread ? thread.node(m[1]) || null : null;
    },
    querySelectorAll: (s) => (s === '[data-reply-mode]' && dom.pair ? dom.pair.radios : []),
  };
  dom.done = () => { document.createElement = realCreate; };
  return dom;
}

await ok('the line, the choice and the button follow the thread: asked again after every change', async () => {
  // A 1:1 so far: Ann wrote to the user alone.
  const first = mail('a1', 'in', '2026-11-20T08:00:00Z', { recipients: [ACCT], cc: [] });
  const narrow = { group: false, participants: [PEOPLE[0]] };
  const el = await open(narrow, [first], [], SOLO());
  const dom = composerDom(el);
  try {
    assert.equal(dom.pair, null);
    // A poll that changes nothing asks nothing.
    assert.equal(await poll(el, [first], [], narrow), false);
    // Ann's next mail copies the boss: the people, the line, and now a choice.
    const BOSS = { address: 'boss@corp.example', name: null, contact: null };
    const second = mail('a2', 'in', '2026-11-20T09:00:00Z', { recipients: [ACCT], cc: ['boss@corp.example'] });
    const wider = { group: true, participants: [PEOPLE[0], { address: 'boss@corp.example', name: null, contact: null }] };
    assert.equal(await poll(el, [first, second], [], wider, PV({ all: { to: [ANN], cc: [BOSS] } })), true, 'a new mail');
    assert.ok(dom.line.html.includes('Cc <span class="rcp"><b>boss@corp.example</b></span>'), dom.line.html);
    assert.ok(dom.pair && dom.pair.html.includes('Sender only'), 'the choice appears, not only its checked state');
    assert.deepEqual(dom.pair.radios.map((r) => r.checked), [true, false]);
    assert.equal(dom.pair.changes.length, 2, 'and is wired');
    assert.equal(dom.send.getAttribute('aria-label'), 'Send the reply to Ann Example <ann@example.com>, copy to boss@corp.example (Ctrl+Enter or ⌘+Enter)');
    assert.deepEqual(el._chat.participants.map((p) => p.address), ['ann@example.com', 'boss@corp.example'], 'the people are the server\'s');
    assert.equal(el._chat.group, true);
    // The user picks Sender only: the line follows, the button too.
    dom.pair.radios[1].checked = true;
    dom.pair.changes[1]();
    assert.equal(el._replyMode, 'sender');
    assert.ok(!dom.line.html.includes('boss@corp.example'));
    // The mail on screen changes — the rail's overlay had no recipients, the
    // record has: same id, and the page asks again.
    const third = mail('a3', 'in', '2026-11-20T10:00:00Z', { recipients: [], cc: [], original: null });
    assert.equal(await poll(el, [first, second, third], [], wider), true);
    const record = { ...third, cc: ['carla@example.net'] };
    assert.equal(await poll(el, [first, second, record], [], wider, PV({ all: { to: [ANN], cc: [CARLA] } })), true, 'a changed mail');
    assert.equal(el._messages.find((m) => m.id === 'a3').cc[0], 'carla@example.net');
    el._setReplyMode('all');
    assert.ok(dom.line.html.includes('carla@example.net'));
    // The newest mail is removed in a mail client (a tombstone): asked again.
    assert.equal(await poll(el, [first, second, { ...record, removed: true, text: '' }], [], wider, PV({ all: { to: [ANN], cc: [BOSS] } })), true, 'a removed mail');
    assert.ok(dom.line.html.includes('boss@corp.example'));
    // A mail that left the document altogether counts too.
    assert.equal(await poll(el, [first, second], [], wider), true, 'a mail gone from the document');
    // A queued send joining or leaving, a send that failed.
    assert.equal(await poll(el, [first, second], [pend('q1')], wider), true, 'a pending send');
    assert.equal(await poll(el, [first, second], [pend('q1', { status: 'failed', error: 'no' })], wider), true, 'its state');
    assert.equal(await poll(el, [first, second], [pend('q1', { status: 'failed', error: 'no' })], wider), false);
    // The people alone.
    assert.equal(await poll(el, [first, second], [pend('q1', { status: 'failed', error: 'no' })],
                            { ...wider, participants: [PEOPLE[0]] }), true, 'the participants');
    // Nobody left to choose between: the pair goes, a selected Sender only
    // does not stay behind.
    el._setReplyMode('sender');
    assert.equal(await poll(el, [first], [], narrow, SOLO()), true);
    assert.equal(dom.pair, null, 'the choice is taken off the page');
    // The gateway goes away: the line says so and the button is off.
    assert.equal(await poll(el, [first, second], [], wider, [{ error: 'x' }, 502]), true);
    assert.ok(dom.line.html.includes('Recipients unavailable right now'));
    assert.equal(dom.send.getAttribute('aria-disabled'), 'true');
    assert.equal(await poll(el, [first, second], [], wider, PV()), true, 'asked again after a failure');
    assert.equal(dom.send.getAttribute('aria-disabled'), null);
  } finally {
    dom.done();
  }
});

await ok('an answer that a later question overtook is dropped', async () => {
  const el = await open();
  const a = el._fetchPreview();
  const b = el._fetchPreview();
  const [ra, rb] = wire.filter((w) => w.url === PV_URL());
  wire.length = 0;
  rb.answer(SOLO());
  await b;
  ra.answer(PV({ all: { to: [BOB], cc: [] } }));
  await a;
  assert.equal(el._preview.doc.all.to[0].address, 'ann@example.com', 'the newest question\'s answer stands');
});

await ok('the reply goes as the line read at the press: mode, recipients, one key', async () => {
  const el = await open();
  let req = await send(el, 'one', sentMsg('s1', 'one'));
  assert.equal(req.url, SEND_URL);
  assert.deepEqual(Object.keys(req.body).sort(), ['reply', 'request_key', 'text']);
  assert.equal(req.body.reply, 'all');
  assert.match(req.body.request_key, KEY_RE);
  const k1 = req.body.request_key;
  // The choice on screen at the press goes with the mail and resets there.
  el._setReplyMode('sender');
  const p = el._sendChat('two');
  assert.equal(el._replyMode, 'all', 'reset at the press, before any answer');
  await settle();
  answerPreview(el, PV(), 'the preview at the press');
  await settle();
  req = take((w) => w.method === 'POST', 'the send');
  assert.equal(req.body.reply, 'sender');
  assert.notEqual(req.body.request_key, k1, 'another press, another key');
  // A choice made for the next mail while this one is on its way survives
  // this one's answer.
  el._setReplyMode('sender');
  req.answer(sentMsg('s2', 'two', { ts: '2026-11-20T11:01:00Z' }));
  await p;
  await quiet(el);
  assert.equal(el._replyMode, 'sender');
  // Queued counts as sent for the choice.
  await send(el, 'three', { pending: pend('q1') }, 202);
  assert.equal(el._replyMode, 'all');
  // A press that did not get through gets its choice back with its words.
  el._setReplyMode('sender');
  let restored = '';
  el._restoreComposer = (t) => { restored = t; };
  await send(el, 'four', { error: 'no' }, 409);
  assert.equal(el._replyMode, 'sender');
  assert.equal(restored, 'four');
  // No choice on the page: "all" — and both ways are the same mail then.
  const solo = await open({}, BASE, [], SOLO());
  req = await send(solo, 'x', sentMsg('s3', 'x'));
  assert.equal(req.body.reply, 'all');
});

await ok('a press is refused when the recipients are not the ones on screen', async () => {
  const el = await open();
  let restored = '';
  el._restoreComposer = (t) => { restored = t; };
  // Bob wrote meanwhile (the page was in the background and has not polled):
  // a "Sender only" reply would now go to Bob. Nothing is posted.
  el._setReplyMode('sender');
  await refusedPress(el, 'for Ann only', PV({ all: { to: [BOB], cc: [ANN, CARLA] }, sender: { to: [BOB], cc: [] } }));
  assert.equal(restored, 'for Ann only');
  assert.equal(el._replyMode, 'sender', 'the choice is back with the words');
  assert.ok(el._notes.at(-1).startsWith('Not sent: the thread changed and with it whom a reply reaches.'), el._notes.at(-1));
  assert.ok(el._chatComposerHtml().includes('To <span class="rcp"><b>Bob Builder</b>'), 'the line now says where it would go');
  // Only the side that is sent has to match: a change to the other one
  // does not stop a press.
  el._pv = PV({ all: { to: [BOB], cc: [ANN, CARLA] }, sender: { to: [BOB], cc: [] } });
  const req = await send(el, 'for Bob', sentMsg('s1', 'for Bob'), 200, PV({ all: { to: [BOB], cc: [ANN] }, sender: { to: [BOB], cc: [] } }));
  assert.equal(req.body.reply, 'sender');
  // Case and order do not make a difference; one address more does.
  await tell(el, PV());
  const swapped = PV({ all: { to: [{ ...ANN, address: 'ANN@Example.com' }], cc: [CARLA, BOB] } });
  await send(el, 'same people', sentMsg('s2', 'same people'), 200, swapped);
  await tell(el, PV());
  await refusedPress(el, 'one more', PV({ all: { to: [ANN], cc: [BOB, CARLA, { address: 'dora@w.org', name: null, contact: null }] } }));
  assert.equal(restored, 'one more');
  assert.ok(el._chatComposerHtml().includes('dora@w.org'));
  // The recipients cannot be had at the press: nothing leaves.
  for (const down of [[{ error: 'gateway unreachable' }, 502], 'offline', [{}, 404], [{ error: 'No mail left.' }, 409]]) {
    await tell(el, PV());
    await refusedPress(el, 'not now', down);
    assert.equal(restored, 'not now');
    assert.ok(el._notes.at(-1).startsWith('Not sent. '), el._notes.at(-1));
  }
  assert.equal(el._notes.at(-1), 'Not sent. No reply can be sent from here: No mail left.');
  // The thread moves while the recipients are being asked for: the answer is
  // older than the thread on screen, and the press is refused.
  await tell(el, PV());
  const p = el._sendChat('raced');
  await settle();
  const at = take((w) => w.url === PV_URL(), 'the preview at the press');
  const polled = el._poll();
  take((w) => w.url === MSG_URL, 'the poll').answer({ chat: summary(), pending: [],
    messages: clone([...BASE, mail('m9', 'in', '2026-11-20T12:00:00Z', { sender: 'bob@builder.example' })]) });
  await polled;
  at.answer(PV());
  await p;
  assert.equal(wire.filter((w) => w.method === 'POST').length, 0);
  assert.equal(restored, 'raced');
  assert.ok(el._notes.at(-1).startsWith('Not sent: the thread changed'));
  await quiet(el);
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
    assert.equal(input.value, '');
    // A mail reply whose recipients are not known is not submitted at all:
    // the words stay in the field.
    if (!chat.channel) {
      el._preview = { state: 'error', doc: null, error: '', rev: el._threadRev };
      input.value = 'stays';
      wire.length = 0;
      form.fire('submit');
      assert.deepEqual(sent, ['line one\nline two'], 'not sent');
      assert.equal(input.value, 'stays');
      assert.ok(el._notes.at(-1).startsWith('Recipients unavailable right now'));
      assert.equal(asked(el), 1, 'and the server is asked again');
    }
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
  assert.ok(!n.html.includes('data-pend-dismiss') && !n.html.includes('data-pend-copy'), 'nothing to dismiss while it waits');
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
  answerPreview(bare, PV(), 'the preview at the press');
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
  await quiet(el);
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

// ── What became of a send: being sent, failed, refused recipients ────────────
await ok('a send that is on its way, or failed, says so and never just disappears', async () => {
  const sending = pend('q1', { status: 'sending', message_id: 'out-1@me.example.org' });
  const failed = pend('q2', { status: 'failed', error: 'The mail server refused the sender <me@example.org>: 550 not allowed.' });
  const unknown = pend('q3', { status: 'failed', outcome_unknown: true, error: 'The connection dropped after the mail was handed over.',
                               refused: ['bob@builder.example'] });
  const el = await open({}, BASE, [sending, failed, unknown, pend('q4')]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q1', 'P:q2', 'P:q3', 'P:q4']);
  // Being sent: said in words; nothing to approve, nothing to dismiss.
  let h = el._thread.pnode('q1').html;
  assert.ok(h.startsWith('<div class="msg out pending sending by-user" data-pending="q1" role="group" ' +
    'aria-label="Sending — not confirmed yet: your mail to ann@example.com">'), h);
  assert.ok(h.includes('<small class="who">You &middot; on its way</small>'));
  assert.ok(h.includes('<span class="pend-badge">Sending — not confirmed yet</span>'));
  for (const no of ['data-approve-slot', 'data-pend-copy', 'data-pend-dismiss']) assert.ok(!h.includes(no), no);
  assert.equal(el._thread.pnode('q1')._slot, null, 'no approval link is mounted');
  // Failed: the reason as text, and the two controls.
  h = el._thread.pnode('q2').html;
  assert.ok(h.startsWith('<div class="msg out pending failed by-user" data-pending="q2" role="group" ' +
    'aria-label="Not sent: your mail to ann@example.com">'), h);
  assert.ok(h.includes('<small class="who">You &middot; not sent</small>'));
  assert.ok(h.includes('<span class="pend-badge">Not sent</span><span class="pend-act">' +
    '<button type="button" class="pend-btn" data-pend-copy>Copy text back to the composer</button>' +
    '<button type="button" class="pend-btn" data-pend-dismiss title="Take this failed mail off the thread">Dismiss</button></span>'), h);
  assert.ok(h.includes('<div class="pend-why">The mail server refused the sender &lt;me@example.org&gt;: 550 not allowed.</div>'), h);
  assert.ok(h.includes('queued q2'), 'the words stay on the page');
  // Outcome unknown: it may have gone out, and the bubble says exactly that.
  h = el._thread.pnode('q3').html;
  assert.ok(h.includes('aria-label="Not confirmed — this mail may have gone out: your mail to ann@example.com"'), h);
  assert.ok(h.includes('<span class="pend-badge">Not confirmed — this mail may have gone out</span>'));
  assert.ok(h.includes('<div class="pend-why">The connection dropped after the mail was handed over. ' +
    'Check with the recipient or the Sent folder before you send it again.</div>'), h);
  assert.ok(!h.includes('>Not sent<'), 'never called not sent');
  assert.ok(h.includes('<div class="msg-refused" role="note">Not delivered to bob@builder.example &mdash; the mail server refused this address.</div>'), h);
  assert.ok(h.includes('data-pend-dismiss'));
  // A failure without a sentence still says something.
  assert.ok(el._pendingHtml(pend('x', { status: 'failed' })).includes('<div class="pend-why">The server gave no reason.</div>'));
  assert.ok(el._pendingHtml(pend('x', { status: 'failed', outcome_unknown: true }))
    .includes('The mail server did not say whether it took it. Check with the recipient'));
  // A state the page does not know reads as waiting, with the approval link.
  assert.ok(el._pendingHtml(pend('x', { status: 'exploded' })).includes('<span class="pend-badge">Waiting for approval</span>'));
  // A queued send that is let go and fails is redrawn where it is.
  const node = el._thread.pnode('q4');
  await poll(el, BASE, [sending, failed, unknown, pend('q4', { status: 'failed', error: 'Rejected by the mail server.' })]);
  assert.notEqual(el._thread.pnode('q4'), node);
  assert.ok(el._thread.pnode('q4').html.includes('Rejected by the mail server.'));
  assert.deepEqual(el._thread.layout().slice(-4), ['P:q1', 'P:q2', 'P:q3', 'P:q4'], 'in its place');
  // The one being sent goes out: its mail takes its place.
  await poll(el, [...BASE, mail('out-1@me.example.org', 'out', '2026-11-20T10:01:00Z', { author: 'user', sender: ACCT })],
             [failed, unknown, pend('q4', { status: 'failed', error: 'Rejected by the mail server.' })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'out-1@me.example.org', 'P:q2', 'P:q3', 'P:q4']);
});

await ok('a failed send: its text back into the composer, and Dismiss', async () => {
  const failed = pend('q/2 x', { status: 'failed', error: 'no', text: 'the failed words' });
  const el = await open({}, BASE, [failed, pend('q3')]);
  // Copy: after what the box holds, never over it; the bubble stays.
  const into = [];
  el._restoreComposer = (t) => { into.push(t); el._draft = t; };
  el._draft = '';
  el._copyPending('q/2 x');
  assert.deepEqual(into, ['the failed words']);
  el._draft = 'already typing  ';
  el._copyPending('q/2 x');
  assert.equal(into.at(-1), 'already typing\n\nthe failed words');
  assert.deepEqual(el._thread.layout().slice(-2), ['P:q/2 x', 'P:q3']);
  // Neither control acts on a send that only waits.
  el._copyPending('q3');
  assert.equal(into.length, 2);
  wire.length = 0;
  await el._dismissPending('q3');
  assert.equal(wire.length, 0);
  // Dismiss fails: it stays, with the reason; nothing is lost.
  let p = el._dismissPending('q/2 x');
  let req = take((w) => w.method === 'POST', 'the dismiss');
  assert.equal(req.url, `/chats/${encodeURIComponent(ID)}/pending/${encodeURIComponent('q/2 x')}/dismiss`);
  el._dismissPending('q/2 x');
  assert.equal(wire.filter((w) => w.method === 'POST').length, 0, 'one press, one request');
  req.answer({ error: 'cross-origin request <refused>' }, 403);
  await p;
  assert.deepEqual(el._thread.layout().slice(-2), ['P:q/2 x', 'P:q3']);
  assert.equal(el._notes.at(-1), 'Not dismissed: cross-origin request &lt;refused&gt;');
  p = el._dismissPending('q/2 x');
  take((w) => w.method === 'POST', 'the dismiss').fail();
  await p;
  assert.equal(el._notes.at(-1), "Couldn't dismiss it &mdash; check the connection and try again.");
  assert.ok(el._thread.pnode('q/2 x'));
  // Dismissed: it leaves, focus does not fall to the document, and a poll
  // that left before it does not bring it back.
  const lagging = el._poll();
  const lag = take((w) => w.url === MSG_URL, 'a poll from before');
  document.activeElement = el._thread.pnode('q/2 x');
  el._thread.pnode('q/2 x').contains = (x) => x === document.activeElement;
  p = el._dismissPending('q/2 x');
  take((w) => w.method === 'POST', 'the dismiss').answer({ ok: true });
  await p;
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q3']);
  assert.equal(globalThis.focused, el._thread);
  lag.answer({ chat: summary(), messages: clone(BASE), pending: [failed, pend('q3')] });
  await lagging;
  await quiet(el);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q3'], 'not drawn again');
  // One that is gone already (404) is dismissed as well.
  const b = await open({}, BASE, [failed]);
  p = b._dismissPending('q/2 x');
  take((w) => w.method === 'POST', 'the dismiss').answer({}, 404);
  await p;
  assert.deepEqual(b._thread.layout(), ['m1', 'm2', 'm3']);
});

await ok('recipients the mail server refused are named on the mail', async () => {
  const el = await open();
  await send(el, 'to both', sentMsg('s1', 'to both', { refused: ['bob@builder.example', '<carla>@example.net'] }));
  const h = el._thread.node('s1').html;
  assert.ok(h.includes('<div class="msg-refused" role="note">Not delivered to bob@builder.example, &lt;carla&gt;@example.net ' +
    '&mdash; the mail server refused these addresses.</div>'), h);
  assert.ok(el._notes.at(-1).startsWith('Sent &mdash; but the mail server refused bob@builder.example, &lt;carla&gt;@example.net.'));
  // A mail first seen whole, and one that learns it later: redrawn.
  await poll(el, [...BASE, sentMsg('s1', 'to both', { refused: ['bob@builder.example'] })], []);
  assert.ok(el._thread.node('s1').html.includes('refused this address.'));
  assert.ok(!el._thread.node('m3').html.includes('msg-refused'));
});

// ── A mail that may have gone out is never called "not sent" ─────────────────
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

await ok('202 unconfirmed: the bubble stays, "Sending — not confirmed yet", and the words never come back', async () => {
  const el = await open();
  let restored = null;
  el._restoreComposer = (t) => { restored = t; };
  el._draftSaved = 'the words';
  const p = el._sendChat('long\nmail');
  await settle();
  answerPreview(el, PV(), 'the preview at the press');
  await settle();
  const req = take((w) => w.method === 'POST', 'the send');
  const key = req.body.request_key;
  req.answer({ unconfirmed: true, message_id: 'real-2@me.example.org', request_key: key }, 202);
  await p;
  assert.equal(restored, null, 'nothing is put back in the box');
  assert.equal(el._draftSaved, '', 'the shared draft is spent');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1']);
  const h = el._thread.node('local-1').html;
  assert.ok(h.startsWith('<div class="msg out by-user unconfirmed" data-mid="local-1" role="group" aria-label="Not confirmed yet: your mail">'), h);
  assert.ok(h.includes('<span class="pend-badge">Sending &mdash; not confirmed yet</span>'), h);
  assert.ok(h.includes('data-recheck'), 'the server knows the key: it can be asked again');
  assert.ok(!el._notes.some((n) => /not sent|couldn't send/i.test(n)), el._notes.join(' | '));
  assert.ok(el._notes.at(-1).startsWith('Handed over, but not confirmed yet that it went out.'));
  assert.equal(el._replyMode, 'all');
  // The page asks the server what it is still sending, at once.
  assert.ok(wire.some((w) => w.url === MSG_URL && w.method === 'GET'));
  await quiet(el);
  // The mail shows: it takes the bubble's place, by its Message-ID.
  const rec = mail('real-2@me.example.org', 'out', '2026-11-20T10:00:05Z', { text: 'long', author: 'user', sender: ACCT });
  await poll(el, [...BASE, rec], []);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'real-2@me.example.org'], 'one bubble, not two');
  assert.equal(el._unconfirmed.length, 0);
  // Without a Message-ID nothing is matched by words: a mail with the very
  // same text is another bubble, and the unconfirmed one stays.
  const b = await open();
  await send(b, 'same words', { unconfirmed: true, request_key: 'someone-elses-key-0000' }, 202);
  assert.ok(!b._thread.node('local-1').html.includes('data-recheck'), 'a server that did not echo the key is not asked again');
  await poll(b, [...BASE, mail('x@y', 'out', '2026-11-20T10:00:05Z', { text: 'same words', author: 'user', sender: ACCT })], []);
  assert.deepEqual(b._thread.layout(), ['m1', 'm2', 'm3', 'x@y', 'local-1'], 'two bubbles: nothing was matched by words');
  // The mail was in the thread before the answer came: nothing to keep.
  const c = await open();
  const pc = c._sendChat('quick');
  await settle();
  answerPreview(c, PV(), 'the preview at the press');
  await settle();
  const rc = take((w) => w.method === 'POST', 'the send');
  await poll(c, [...BASE, mail('real-3@me.example.org', 'out', '2026-11-20T10:00:05Z', { text: 'quick', author: 'user', sender: ACCT })], []);
  rc.answer({ unconfirmed: true, message_id: 'real-3@me.example.org', request_key: rc.body.request_key }, 202);
  await pc;
  assert.deepEqual(c._thread.layout(), ['m1', 'm2', 'm3', 'real-3@me.example.org']);
  assert.equal(c._unconfirmed.length, 0);
});

await ok('an unconfirmed mail gives way to what the server says became of it', async () => {
  // By Message-ID: the gateway lists it as still being sent, then as failed.
  const el = await open();
  await send(el, 'hello', { unconfirmed: true, message_id: 'real-4@me.example.org', request_key: 'k' }, 202);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1']);
  await poll(el, BASE, [pend('g1', { status: 'sending', text: 'hello', message_id: 'real-4@me.example.org' })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:g1'], 'the server\'s entry, not two bubbles');
  assert.equal(el._unconfirmed.length, 0);
  await poll(el, BASE, [pend('g1', { status: 'failed', outcome_unknown: true, error: 'No answer from the mail server.', text: 'hello',
                                     message_id: 'real-4@me.example.org' })]);
  assert.ok(el._thread.pnode('g1').html.includes('may have gone out'));
  // By the request_key, where no Message-ID was known.
  const b = await open();
  const p = b._sendChat('hello');
  await settle();
  answerPreview(b, PV(), 'the preview at the press');
  await settle();
  const req = take((w) => w.method === 'POST', 'the send');
  req.answer({ unconfirmed: true, request_key: req.body.request_key }, 202);
  await p;
  await quiet(b);
  await poll(b, BASE, [pend('g2', { status: 'failed', error: 'Refused.', text: 'hello', request_key: req.body.request_key })]);
  assert.deepEqual(b._thread.layout(), ['m1', 'm2', 'm3', 'P:g2']);
});

await ok('Check again asks about the same press, and never sends a second mail', async () => {
  const start = async () => {
    const el = await open();
    el._into = [];
    el._restoreComposer = (t) => { el._into.push(t); el._draft = t; };
    const p = el._sendChat('the mail');
    await settle();
    answerPreview(el, PV(), 'the preview at the press');
    await settle();
    const req = take((w) => w.method === 'POST', 'the send');
    req.answer({ unconfirmed: true, request_key: req.body.request_key }, 202);
    await p;
    await quiet(el);
    return { el, first: req };
  };
  // The recipients are asked for first (answered as the page was told
  // last), then the question leaves.
  const again = async (el) => {
    const p = el._recheck('local-1');
    await settle();
    assert.equal(wire.filter((w) => w.method === 'POST').length, 0, 'nothing is posted before the recipients are known');
    answerPreview(el, el._pv, 'the preview before the question');
    await settle();
    const req = take((w) => w.method === 'POST', 'the question');
    el._recheck('local-1');
    await settle();
    assert.equal(wire.filter((w) => w.method === 'POST').length, 0, 'one question at a time');
    assert.equal(asked(el), 0, 'one question at a time');
    return { p, req };
  };
  // Sent: the very same request went out again, and the answer is the mail.
  let { el, first } = await start();
  let q = await again(el);
  assert.equal(q.req.url, SEND_URL);
  assert.deepEqual(q.req.body, first.body, 'the same body under the same request_key');
  q.req.answer(sentMsg('real-5@me.example.org', 'the mail'));
  await q.p;
  await quiet(el);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'real-5@me.example.org']);
  assert.equal(el._unconfirmed.length, 0);
  assert.deepEqual(el._into, []);
  // It waits for approval after all.
  ({ el, first } = await start());
  q = await again(el);
  q.req.answer({ pending: pend('q1', { text: 'the mail' }) }, 202);
  await q.p;
  await quiet(el);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q1']);
  // The mail gateway's word is that it did not go: now, and only now, the
  // words come back — after what the box holds. Its word on this press is
  // an answer that says it is its own (`"by": "gateway"`): a 424 (the send
  // failed for certain), a refusal, a 503 that says nothing was sent.
  for (const [doc, status, note] of [
    [{ error: 'The mail server refused it.', sent: false, by: 'gateway' }, 424,
     'Not sent: The mail server refused it. The text is back in the box.'],
    [{ by: 'gateway' }, 424, 'Not sent: the server refused it (424). The text is back in the box.'],
    [{ error: 'This thread has no mail left to reply to.', request_key: 'KEY', by: 'gateway' }, 409,
     'Not sent: This thread has no mail left to reply to. The text is back in the box.'],
    [{ error: 'the mail gateway is not configured', sent: false, request_key: 'KEY', by: 'gateway' }, 503,
     'Not sent: the mail gateway is not configured The text is back in the box.'],
  ]) {
    ({ el, first } = await start());
    el._draft = 'typed since';
    q = await again(el);
    q.req.answer(doc.request_key ? { ...doc, request_key: first.body.request_key } : doc, status);
    await q.p;
    await quiet(el);
    assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3'], String(status));
    assert.deepEqual(el._into, ['typed since\n\nthe mail']);
    assert.equal(el._notes.at(-1), note);
    assert.equal(el._unconfirmed.length, 0);
  }
  // Still unknown — by the server's word, or because it cannot be reached:
  // the bubble stays and nothing is sent again.
  ({ el, first } = await start());
  assert.ok(!el._thread.node('local-1').html.includes('data-unsure-'), 'no way out is offered before the server was asked');
  q = await again(el);
  q.req.answer({ unconfirmed: true, message_id: 'real-6@me.example.org', request_key: first.body.request_key }, 202);
  await q.p;
  assert.equal(el._notes.at(-1), 'Still not confirmed. Nothing was sent a second time.');
  assert.equal(el._unconfirmed[0].messageId, 'real-6@me.example.org', 'what it learned is kept');
  q = await again(el);
  q.req.fail();
  await q.p;
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1']);
  assert.deepEqual(el._into, []);
  assert.equal(el._notes.at(-1), 'The server could not be asked about this mail. It is still not confirmed; nothing was sent a second time.');
  // An answer that is about this request, not about the first one, says
  // nothing of the mail: the dashboard's own (a gateway that could not be
  // reached now — "sent": false, nothing was sent NOW —, no gateway for the
  // mailbox, a sign-in that ran out, another site's request refused), a
  // proxy's error, and a server too old to say whose word it is — whose
  // 424 might be the dashboard's as well. None of them is "Not sent".
  for (const [doc, status, why] of [
    [{ error: 'The mail gateway could not be reached; nothing was sent.', sent: false, request_key: 'KEY', by: 'dashboard' }, 502,
     ' (The mail gateway could not be reached; nothing was sent.)'],
    [{ error: 'The mail gateway could not be reached; nothing was sent.', sent: false, request_key: 'KEY' }, 502,
     ' (The mail gateway could not be reached; nothing was sent.)'],
    [{ error: 'the mail gateway is not configured', sent: false, request_key: 'KEY' }, 503, ' (the mail gateway is not configured)'],
    [{ error: 'send failed: refused', sent: false, request_key: 'KEY' }, 424, ' (send failed: refused)'],
    [{ error: 'no mail gateway serves that mailbox', sent: false, request_key: 'KEY', by: 'dashboard' }, 409,
     ' (no mail gateway serves that mailbox)'],
    [{ error: 'This thread has no mail left to reply to.', request_key: 'KEY' }, 409, ' (This thread has no mail left to reply to.)'],
    [{}, 401, ' (it answered 401)'],
    [{ error: 'cross-site request rejected', by: 'dashboard' }, 403, ' (cross-site request rejected)'],
    [{ error: 'no gateway serves this chat' }, 409, ' (no gateway serves this chat)'],
    [{ error: 'odd', by: 'Gateway' }, 409, ' (odd)'],
    [null, 500, ' (it answered 500)'],
  ]) {
    q = await again(el);
    q.req.answer(doc && doc.request_key ? { ...doc, request_key: first.body.request_key } : doc, status);
    await q.p;
    assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1'], String(status));
    assert.deepEqual(el._into, [], String(status));
    assert.equal(el._notes.at(-1), `The server could not be asked about this mail${why}. ` +
      'It is still not confirmed; nothing was sent a second time.');
    assert.ok(!el._notes.at(-1).startsWith('Not sent'));
  }
  // Asked without an end: the bubble now offers its words back, and a way
  // off the thread, beside the question.
  const stuck = el._thread.node('local-1').html;
  assert.ok(stuck.includes('data-recheck') && stuck.includes('data-unsure-copy') && stuck.includes('data-unsure-dismiss'), stuck);
  // The mail arrives on its own: asking again has nothing left to ask about.
  await poll(el, [...BASE, mail('real-6@me.example.org', 'out', '2026-11-20T10:00:05Z', { author: 'user', sender: ACCT })], []);
  wire.length = 0;
  await el._recheck('local-1');
  assert.equal(wire.length, 0);
});

await ok('Check again never sends to recipients the press did not name', async () => {
  // The gateway may never have had the key: the question is then the first
  // request to send the mail, addressed from the thread as it stands now.
  const start = async (mode = 'all') => {
    const el = await open();
    el._into = [];
    el._restoreComposer = (t) => { el._into.push(t); el._draft = t; };
    if (mode !== 'all') el._setReplyMode(mode);
    const p = el._sendChat('the mail');
    await settle();
    answerPreview(el, PV(), 'the preview at the press');
    await settle();
    const req = take((w) => w.method === 'POST', 'the send');
    req.answer({ unconfirmed: true, request_key: req.body.request_key }, 202);
    await p;
    await quiet(el);
    return { el, first: req };
  };
  const posts = () => wire.filter((w) => w.method === 'POST').length;
  // Bob has written since, and a reply to the sender now goes to him.
  const BOB_NOW = PV({ sender: { to: [BOB], cc: [] } });
  let { el, first } = await start('sender');
  assert.equal(first.body.reply, 'sender');
  let p = el._recheck('local-1');
  await settle();
  answerPreview(el, BOB_NOW, 'the preview before the question');
  await p;
  assert.equal(posts(), 0, 'nothing is posted: it would reach Bob, whom the line never named');
  assert.ok(el._notes.at(-1).startsWith('Not asked: the thread has changed, and a reply now reaches other recipients'), el._notes.at(-1));
  assert.ok(!/not sent/i.test(el._notes.at(-1)), 'nothing here says it did not go');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1'], 'the bubble stays');
  // And is not asked again from here, even when the recipients are back:
  // the bubble offers its words instead of the question.
  let h = el._thread.node('local-1').html;
  assert.ok(!h.includes('data-recheck'), h);
  assert.ok(h.includes('<span class="pend-badge">Not confirmed &mdash; this mail may have gone out</span>'), h);
  assert.ok(h.includes('data-unsure-copy>Copy text back to the composer</button>') && h.includes('data-unsure-dismiss'), h);
  wire.length = 0;
  await el._recheck('local-1');
  assert.equal(wire.length, 0, 'no request at all');
  // The other mode is compared on its own side: Reply all still reaches
  // the same people, and that press may be asked about.
  ({ el, first } = await start('all'));
  p = el._recheck('local-1');
  await settle();
  answerPreview(el, BOB_NOW, 'the preview before the question');
  await settle();
  const q = take((w) => w.method === 'POST', 'the question');
  assert.deepEqual(q.body, first.body);
  q.answer(sentMsg('real-9@me.example.org', 'the mail'));
  await p;
  await quiet(el);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'real-9@me.example.org']);
  // The recipients cannot be had, or the thread moved while they were
  // asked for: nothing is posted, and the question can be put again.
  for (const at of ['offline', [{ error: 'x' }, 502], [{ error: 'no mail left to reply to' }, 409], [{}, 404], 'moved']) {
    ({ el, first } = await start());
    p = el._recheck('local-1');
    await settle();
    if (at === 'moved') {
      el._bumpThread();
      answerPreview(el, PV(), 'the preview before the question');
    } else {
      answerPreview(el, at, 'the preview before the question');
    }
    await p;
    assert.equal(posts(), 0, JSON.stringify(at));
    assert.equal(el._notes.at(-1), 'Not asked: whom this mail would reach could not be confirmed just now. ' +
      'Nothing was sent; try again in a moment.');
    h = el._thread.node('local-1').html;
    assert.ok(h.includes('data-recheck') && h.includes('data-unsure-copy'), 'still askable, and no dead end');
    await quiet(el);
  }
  // A press from longer ago than the gateway keeps a key is not asked
  // about: the same request would be a new mail.
  ({ el, first } = await start());
  el._unconfirmed[0].at = Date.now() - 13 * 3600 * 1000;
  wire.length = 0;
  await el._recheck('local-1');
  assert.equal(wire.length, 0);
  assert.ok(el._notes.at(-1).startsWith('This mail can no longer be asked about from here.'));
  assert.ok(!el._thread.node('local-1').html.includes('data-recheck'));
});

await ok('an unconfirmed mail nobody can account for has two ways out: its words back, and Dismiss', async () => {
  // A server that did not echo the key cannot be asked: the ways out are
  // there from the start.
  const el = await open();
  el._into = [];
  el._restoreComposer = (t) => { el._into.push(t); el._draft = t; };
  el._draft = 'typed since';
  await send(el, 'lost words', { unconfirmed: true }, 202);
  const h = el._thread.node('local-1').html;
  assert.ok(!h.includes('data-recheck') && h.includes('data-unsure-copy') && h.includes('data-unsure-dismiss'), h);
  // The words back: after what the box holds, the bubble still there.
  el._copyUnconfirmed('local-1');
  assert.deepEqual(el._into, ['typed since\n\nlost words']);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1']);
  assert.ok(el._notes.at(-1).startsWith('The text is back in the box.'));
  // Dismiss: off the thread, no request (the bubble is the page's own), and
  // focus on its button does not fall to the document.
  el._thread.node('local-1').focus();
  wire.length = 0;
  el._dismissUnconfirmed('local-1');
  assert.equal(wire.length, 0);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  assert.equal(el._unconfirmed.length, 0);
  assert.equal(globalThis.focused, el._thread, 'focus goes to the thread');
  assert.equal(el._notes.at(-1), 'Dismissed. If that mail did go out, it shows in the thread.');
  el._dismissUnconfirmed('local-1');
  el._copyUnconfirmed('local-1');
  assert.deepEqual(el._into, ['typed since\n\nlost words'], 'nothing left to act on');
});

await ok('an unconfirmed mail survives a reload on the device until it is accounted for or dismissed', async () => {
  const KEPT = `retinue-draft:chat-unsure:${ID}`;
  const kept = () => { const r = JSON.parse(store.get(KEPT) || 'null'); return r ? r.v : null; };
  // The web gateway's hop broke after the request was written and the mail
  // gateway never stored it: 202 {unconfirmed}, no Message-ID, and the
  // queue lists nothing. The words are in no box and no draft.
  const press = async (text = 'the mail', answer = {}) => {
    const el = await open();
    el._keepLocal = Page.prototype._keepLocal;
    el._into = [];
    el._restoreComposer = (t) => { el._into.push(t); el._draft = t; };
    const p = el._sendChat(text);
    await settle();
    answerPreview(el, PV(), 'the preview at the press');
    await settle();
    const req = take((w) => w.method === 'POST', 'the send');
    req.answer({ unconfirmed: true, request_key: req.body.request_key, ...answer }, 202);
    await p;
    await quiet(el);
    return { el, first: req };
  };
  const reload = async (messages = BASE, pending = []) => {
    const el = await open({}, messages, pending, PV(), true);
    el._keepLocal = Page.prototype._keepLocal;
    el._into = [];
    el._restoreComposer = (t) => { el._into.push(t); el._draft = t; };
    return el;
  };
  let { el, first } = await press();
  const key = first.body.request_key;
  assert.equal(kept().list.length, 1, 'kept on the device');
  assert.deepEqual({ ...kept().list[0], at: 0, shown: '' },
    { key, text: 'the mail', mode: 'all', shown: '', at: 0, messageId: '', echoed: true, files: 0, stale: false });
  assert.ok(kept().list[0].shown.includes('ann@example.com'), 'with whom its line named');
  // The tab is discarded, the page reloads: the bubble is there again, with
  // the question and the ways out — not a thread that says nothing.
  el = await reload();
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1']);
  let h = el._thread.node('local-1').html;
  assert.ok(h.includes('<span class="pend-badge">Sending &mdash; not confirmed yet</span>'), h);
  assert.ok(h.includes('data-recheck') && h.includes('data-unsure-copy') && h.includes('data-unsure-dismiss'), h);
  assert.ok(h.includes('the mail'));
  assert.equal(el._draft, '', 'the words are not put back in the box unasked');
  assert.equal(kept().list.length, 1, 'and it stays kept');
  // Check again asks as the press asked — the same words, choice and key —
  // after the recipients, and the gateway's word ends it: not sent, with
  // the words offered back.
  let p = el._recheck('local-1');
  await settle();
  answerPreview(el, PV(), 'the preview before the question');
  await settle();
  let q = take((w) => w.method === 'POST', 'the question');
  assert.equal(q.url, SEND_URL);
  assert.deepEqual(q.body, first.body, 'the very request of the press');
  q.answer({ error: 'send failed: the mail server refused it', sent: false, request_key: key, by: 'gateway' }, 424);
  await p;
  await quiet(el);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  assert.deepEqual(el._into, ['the mail']);
  assert.equal(el._notes.at(-1), 'Not sent: send failed: the mail server refused it The text is back in the box.');
  assert.ok(!kept(), 'the device lets go of it');
  // The gateway never had the key and the mail is sent by the question:
  // the mail takes the bubble's place.
  ({ el, first } = await press());
  el = await reload();
  p = el._recheck('local-1');
  await settle();
  answerPreview(el, PV(), 'the preview before the question');
  await settle();
  take((w) => w.method === 'POST', 'the question').answer(sentMsg('late@me.example.org', 'the mail'));
  await p;
  await quiet(el);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'late@me.example.org']);
  assert.ok(!kept());
  // After a reload the question is held to the recipients of the press too.
  ({ el, first } = await press());
  el = await reload();
  p = el._recheck('local-1');
  await settle();
  answerPreview(el, PV({ all: { to: [BOB], cc: [] } }), 'the preview before the question');
  await p;
  assert.equal(wire.filter((w) => w.method === 'POST').length, 0);
  assert.equal(kept().list[0].stale, true, 'kept: the next reload does not offer the question again');
  el = await reload();
  assert.ok(!el._thread.node('local-1').html.includes('data-recheck'));
  // Accounted for while the page was away — the mail is in the thread (by
  // its Message-ID), or the server lists the send (by Message-ID or key):
  // no bubble of the page's own, and nothing kept.
  ({ el, first } = await press('the mail', { message_id: 'mid-1@me.example.org' }));
  assert.equal(kept().list[0].messageId, 'mid-1@me.example.org');
  el = await reload([...BASE, mail('mid-1@me.example.org', 'out', '2026-11-20T10:00:05Z', { author: 'user', sender: ACCT })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'mid-1@me.example.org']);
  assert.equal(el._unconfirmed.length, 0);
  assert.ok(!kept());
  ({ el, first } = await press());
  el = await reload(BASE, [pend('g9', { status: 'failed', outcome_unknown: true, error: 'No answer.', text: 'the mail',
                                       request_key: first.body.request_key })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:g9'], 'the server\'s entry speaks for it');
  assert.equal(el._unconfirmed.length, 0);
  // A mail with the same words is not it: nothing is matched by words.
  ({ el, first } = await press());
  el = await reload([...BASE, mail('x@y', 'out', new Date().toISOString(), { text: 'the mail', author: 'user', sender: ACCT })]);
  assert.ok(el._thread.layout().includes('local-1'));
  // Dismissed: gone from the thread and from the device, also after a reload.
  el._dismissUnconfirmed('local-1');
  assert.ok(!kept());
  el = await reload();
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  // It stands in the thread where it was sent: a mail that arrived since
  // comes after it.
  ({ el, first } = await press());
  const later = mail('m9', 'in', new Date(Date.now() + 5 * 60 * 1000).toISOString());
  el = await reload([...BASE, later]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1', 'm9']);
  // A mail with attachments cannot be asked about again (the files are not
  // kept), nor one whose server did not echo the key, nor one from longer
  // ago than a key is known: the ways out, no question.
  const rec = { key: 'k'.repeat(32), text: 'kept text', mode: 'all', shown: kept().list[0].shown, at: Date.now() - 1000,
                messageId: '', echoed: true, files: 0, stale: false };
  const keep = (list) => store.set(KEPT, JSON.stringify({ at: Date.now(), v: { list } }));
  for (const odd of [{ files: 2 }, { echoed: false }, { at: Date.now() - 13 * 3600 * 1000 }, { shown: '' }, { stale: true }]) {
    keep([{ ...rec, ...odd }]);
    el = await reload();
    h = el._thread.node('local-1').html;
    assert.ok(!h.includes('data-recheck') && h.includes('data-unsure-copy') && h.includes('data-unsure-dismiss'), JSON.stringify(odd));
    assert.ok(h.includes('<span class="pend-badge">Not confirmed &mdash; this mail may have gone out</span>'), h);
    wire.length = 0;
    await el._recheck('local-1');
    assert.equal(wire.length, 0, JSON.stringify(odd));
  }
  // What is kept is read as data: a record that is none draws nothing, and
  // the same press is not drawn twice.
  keep([{ ...rec, key: '../../etc' }, { ...rec, text: 7 }, { ...rec, at: 'now' }, { ...rec, at: Date.now() + 3600 * 1000 },
        null, 'k', rec, rec]);
  el = await reload();
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1']);
  assert.ok(el._thread.node('local-1').html.includes('kept text'));
  keep('nonsense');
  el = await reload();
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  // A time no date can be made of is none: the chat opens, with the rest.
  keep([{ ...rec, key: 'a'.repeat(32), at: -1e17 }, { ...rec, key: 'b'.repeat(32), at: 0 },
        { ...rec, key: 'c'.repeat(32), at: -1 }, rec]);
  el = await reload();
  assert.equal(el._state, 'ok');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1']);
  assert.ok(el._thread.node('local-1').html.includes('kept text'));
  // And whatever else goes wrong with the record, the chat opens — without
  // its bubbles, and with the record left for a page that can read it.
  keep([rec]);
  const redraw = Page.prototype._redrawUnconfirmed;
  Page.prototype._redrawUnconfirmed = function () { redraw.call(this); throw new RangeError('bad record'); };
  try {
    el = await reload();
  } finally {
    Page.prototype._redrawUnconfirmed = redraw;
  }
  assert.equal(el._state, 'ok');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  assert.deepEqual(el._unconfirmed, []);
  assert.equal(kept().list.length, 1, 'the words are in no other place: kept');
  // Its text is text: escaped like any bubble's.
  keep([{ ...rec, text: '<img src=x onerror=alert(1)>' }]);
  el = await reload();
  h = el._thread.node('local-1').html;
  assert.ok(h.includes('&lt;img src=x onerror=alert(1)&gt;') && !h.includes('<img'), h);
  // The list is a record of its own, apart from the composer's: letting go
  // of a mail as the page loads costs the box neither the words the device
  // kept for it nor the press kept with them — they are read back after.
  const BOX = `retinue-draft:chat:${ID}`;
  const box = () => { const r = JSON.parse(store.get(BOX) || 'null'); return r ? r.v : null; };
  const retry = { key: 'r'.repeat(32), text: 'unsaved words', mode: 'all', imgs: '[]', at: Date.now() - 1000, seen: 0 };
  const keepBox = () => store.set(BOX, JSON.stringify({ at: Date.now(),
    v: { text: 'unsaved words', version: 0, byAra: false, sending: null, retry } }));
  const arrived = [...BASE, mail('mid-2@me.example.org', 'out', '2026-11-20T10:00:05Z', { author: 'user', sender: ACCT })];
  keep([{ ...rec, messageId: 'mid-2@me.example.org' }]);
  keepBox();
  realKeep = true;
  try {
    el = await open({}, arrived, [], PV(), true);
    assert.equal(el._unconfirmed.length, 0, 'the mail is in the thread');
    assert.ok(!kept());
    assert.equal(el._draft, 'unsaved words', 'the words the device kept are back in the box');
    assert.equal(el._retry && el._retry.key, retry.key, 'with their press');
    assert.equal(box().text, 'unsaved words');
    // Nor while the chat cannot be written to: its words wait on the device.
    keep([{ ...rec, messageId: 'mid-2@me.example.org' }]);
    keepBox();
    el = await open({ can_send: false, cannot_send_reason: 'The mail gateway is not connected.' }, arrived, [], PV(), true);
    assert.equal(el._unconfirmed.length, 0);
    assert.ok(!kept());
    assert.deepEqual([box().text, box().retry.key], ['unsaved words', retry.key], 'untouched');
    // And an unconfirmed mail does not make the box's record stay: nothing
    // unsaved, nothing kept there.
    ({ el, first } = await press());
    assert.equal(kept().list.length, 1);
    assert.equal(box(), null);
  } finally {
    realKeep = false;
  }
});

await ok('where the chat cannot be written to, the words of a bubble go to the clipboard', async () => {
  const KEPT = `retinue-draft:chat-unsure:${ID}`;
  const rec = { key: 'k'.repeat(32), text: 'kept words', mode: 'all', shown: 'x', at: Date.now() - 1000,
                messageId: '', echoed: true, files: 0, stale: false };
  const off = { can_send: false, cannot_send_reason: 'The mail gateway is not connected.' };
  const copied = [];
  let works = true;
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
    writeText: async (t) => { if (!works) throw new Error('denied'); copied.push(t); } } });
  try {
    store.clear();
    store.set(KEPT, JSON.stringify({ at: Date.now(), v: { list: [rec] } }));
    let el = await open(off, BASE, [pend('f1', { status: 'failed', error: 'Refused.', text: 'failed words' })], PV(), true);
    const h = el._thread.node('local-1').html;
    assert.ok(h.includes('data-unsure-copy') && h.includes('>Copy text</button>') && !h.includes('back to the composer'), h);
    assert.ok(el._thread.pnode('f1').html.includes('>Copy text</button>'));
    await el._copyUnconfirmed('local-1');
    assert.deepEqual(copied, ['kept words']);
    assert.equal(el._notes.at(-1), 'The text is copied. This chat cannot be written to just now. '
      + 'If this mail shows up in the thread, do not send it again.');
    assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1', 'P:f1'], 'the bubbles stay');
    assert.equal(el._draft, '');
    await el._copyPending('f1');
    assert.deepEqual(copied, ['kept words', 'failed words']);
    assert.ok(el._notes.at(-1).startsWith('The text is copied.'), el._notes.at(-1));
    // A clipboard that refuses: said, and the words stay in the bubble.
    works = false;
    await el._copyUnconfirmed('local-1');
    assert.ok(el._notes.at(-1).startsWith('The text could not be copied here.'), el._notes.at(-1));
    assert.equal(JSON.parse(store.get(KEPT)).v.list.length, 1);
    // Where it can be written to, the words go back into the box as before.
    el = await open({}, BASE, [], PV(), true);
    el._into = [];
    el._restoreComposer = (t) => { el._into.push(t); el._draft = t; };
    assert.ok(el._thread.node('local-1').html.includes('Copy text back to the composer'));
  } finally {
    delete navigator.clipboard;
  }
});

await ok('two tabs on one chat: neither erases the other\'s unconfirmed mail', async () => {
  const KEPT = `retinue-draft:chat-unsure:${ID}`;
  const kept = () => { const r = JSON.parse(store.get(KEPT) || 'null'); return r ? r.v.list.map((x) => x.text) : []; };
  const press = async (el, text) => {
    const p = el._sendChat(text);
    await settle();
    answerPreview(el, PV(), 'the preview at the press');
    await settle();
    const req = take((w) => w.method === 'POST', 'the send');
    req.answer({ unconfirmed: true, request_key: req.body.request_key }, 202);
    await p;
    await quiet(el);
  };
  // Both tabs read the record before either pressed.
  const a = await open();
  a._restoreComposer = (t) => { a._draft = t; };
  const b = await open({}, BASE, [], PV(), true);
  b._restoreComposer = (t) => { b._draft = t; };
  await press(a, 'from tab A');
  await press(b, 'from tab B');
  assert.deepEqual(kept(), ['from tab A', 'from tab B']);
  // A reload shows both.
  let c = await open({}, BASE, [], PV(), true);
  assert.deepEqual(c._unconfirmed.map((u) => u.text), ['from tab A', 'from tab B']);
  // What a tab lets go of goes; what it never held stays.
  b._dismissUnconfirmed(b._unconfirmed[0].id);
  assert.deepEqual(kept(), ['from tab A']);
  await press(b, 'again from tab B');
  assert.deepEqual(kept(), ['from tab A', 'again from tab B']);
  a._dismissUnconfirmed(a._unconfirmed[0].id);
  assert.deepEqual(kept(), ['again from tab B']);
  // A tab that held a mail since its load and let it go does not bring it
  // back either; one it never held is not dropped by its writes.
  c = await open({}, BASE, [], PV(), true);
  c._restoreComposer = (t) => { c._draft = t; };
  assert.deepEqual(c._unconfirmed.map((u) => u.text), ['again from tab B']);
  c._dismissUnconfirmed(c._unconfirmed[0].id);
  assert.deepEqual(kept(), []);
  await press(c, 'from tab C');
  assert.deepEqual(kept(), ['from tab C']);
  await press(b, 'third from tab B');
  assert.deepEqual(kept(), ['again from tab B', 'from tab C', 'third from tab B'],
    'tab B still holds its own: kept again by its next write, beside the one it never held');
});

await ok('focus on an unconfirmed bubble goes with what takes its place', async () => {
  const start = async () => {
    const el = await open();
    el._restoreComposer = (t) => { el._draft = t; };
    const p = el._sendChat('hello');
    await settle();
    answerPreview(el, PV(), 'the preview at the press');
    await settle();
    const req = take((w) => w.method === 'POST', 'the send');
    req.answer({ unconfirmed: true, request_key: req.body.request_key }, 202);
    await p;
    await quiet(el);
    el._thread.node('local-1').focus();
    return { el, key: req.body.request_key };
  };
  // The server's pending entry takes over: its bubble has the focus.
  let { el, key } = await start();
  await poll(el, BASE, [pend('g1', { status: 'sending', text: 'hello', request_key: key })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:g1']);
  assert.equal(globalThis.focused, el._thread.pnode('g1'), 'the bubble that took its place');
  assert.equal(el._thread.pnode('g1').attrs.get('tabindex'), '-1');
  // One that waits for approval: its link.
  ({ el, key } = await start());
  await poll(el, BASE, [pend('g2', { text: 'hello', request_key: key })]);
  assert.equal(globalThis.focused, link(el, 'g2').children[0], 'the approval link');
  // Focus elsewhere stays where it is.
  ({ el, key } = await start());
  const other = { focus() {} };
  document.activeElement = other;
  globalThis.focused = other;
  await poll(el, BASE, [pend('g3', { status: 'sending', text: 'hello', request_key: key })]);
  assert.equal(globalThis.focused, other);
  // Check again answered with a refusal: the bubble goes, the words are in
  // the box, and so is the focus.
  ({ el, key } = await start());
  const box = { focus() { globalThis.focused = box; }, matches: () => true };
  const root = el.shadowRoot;
  const q0 = root.querySelector;
  root.querySelector = (s) => (s === '[data-composer="chat"] textarea' ? box : q0(s));
  let p = el._recheck('local-1');
  await settle();
  answerPreview(el, PV(), 'the preview before the question');
  await settle();
  take((w) => w.method === 'POST', 'the question').answer({ error: 'Refused.', sent: false, by: 'gateway' }, 424);
  await p;
  assert.equal(globalThis.focused, box, 'the composer');
  await quiet(el);
  // Check again answered with the mail the thread already shows: the mail.
  ({ el, key } = await start());
  const there = mail('real-7@me.example.org', 'out', '2026-11-20T10:00:05Z', { text: 'hello', author: 'user', sender: ACCT });
  await poll(el, [...BASE, there], []);
  el._thread.node('local-1').focus();
  p = el._recheck('local-1');
  await settle();
  answerPreview(el, PV(), 'the preview before the question');
  await settle();
  take((w) => w.method === 'POST', 'the question').answer(there);
  await p;
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'real-7@me.example.org']);
  assert.equal(globalThis.focused, el._thread.node('real-7@me.example.org'));
});

await ok('a refused mail says why, and the words come back', async () => {
  const el = await open();
  let restored = '';
  el._restoreComposer = (t) => { restored = t; };
  await send(el, 'hello', { error: 'This thread has no mail left to reply to <here>.' }, 409);
  assert.equal(el._notes.at(-1), 'Not sent: This thread has no mail left to reply to &lt;here&gt;.');
  assert.equal(restored, 'hello');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  assert.equal(el._retry, null, 'a refusal is final: the next press is a new one');
  await send(el, 'bare', {}, 403);
  assert.equal(el._notes.at(-1), 'Not sent: the server refused it (403).');
});

await ok('no answer, or a 5xx, is not "not sent" — and the same press again is the same mail', async () => {
  const el = await open();
  let restored = '';
  el._restoreComposer = (t) => { restored = t; el._draft = t; };
  // The connection drops: the request may have arrived.
  let req = await send(el, 'did it go?', 'offline');
  const key = req.body.request_key;
  assert.equal(restored, 'did it go?');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  assert.equal(el._notes.at(-1), 'No answer came back, so it is not known whether this mail went out. ' +
    'It is back in the box &mdash; if it shows up in the thread, do not send it again.');
  assert.ok(!el._notes.some((n) => /not sent|couldn't send/i.test(n)), el._notes.join(' | '));
  // Pressed again as it is: the same request_key, so the gateway answers
  // from the first time instead of sending a second mail. A 5xx that does
  // not say "sent": false — a proxy between the page and the server, a
  // crash — is no word on the mail either.
  req = await send(el, 'did it go?', { error: 'bad gateway <x>' }, 502);
  assert.equal(req.body.request_key, key);
  assert.equal(el._notes.at(-1), 'The server could not confirm this mail (bad gateway &lt;x&gt;). ' +
    'It is back in the box &mdash; if it shows up in the thread, do not send it again.');
  req = await send(el, 'did it go?', {}, 500);
  assert.equal(req.body.request_key, key);
  assert.ok(el._notes.at(-1).startsWith('The server could not confirm this mail (it answered 500).'));
  // Nor is a "sent" that is not the plain boolean.
  for (const sent of ['false', 0, null, 'no']) {
    req = await send(el, 'did it go?', { error: 'x', sent }, 502);
    assert.equal(req.body.request_key, key, JSON.stringify(sent));
    assert.ok(el._notes.at(-1).startsWith('The server could not confirm this mail (x).'), JSON.stringify(sent));
  }
  // A 200 that is no document is no word on the mail either.
  req = await send(el, 'did it go?', null, 200);
  assert.equal(req.body.request_key, key);
  assert.ok(el._notes.at(-1).startsWith('No answer came back'));
  // Under another choice it is another mail; so are other words.
  el._setReplyMode('sender');
  req = await send(el, 'did it go?', 'offline');
  assert.notEqual(req.body.request_key, key);
  const key2 = req.body.request_key;
  assert.equal(el._replyMode, 'sender', 'the choice came back with the words');
  req = await send(el, 'did it go? (edited)', 'offline');
  assert.notEqual(req.body.request_key, key2);
  // A press refused here, before anything was posted, keeps the key of the
  // press it repeats.
  const key3 = req.body.request_key;
  el._setReplyMode('sender');
  await refusedPress(el, 'did it go? (edited)', [{ error: 'x' }, 502]);
  const again = el._fetchPreview();
  answerPreview(el, PV());
  await again;
  req = await send(el, 'did it go? (edited)', sentMsg('s9', 'did it go? (edited)'));
  assert.equal(req.body.request_key, key3);
  assert.equal(el._retry, null, 'answered: the key is spent');
});

await ok('a send that certainly failed ends its press: "sent": false is "Not sent", and the next press is a new one', async () => {
  // The web gateway's word that nothing went out, whatever its status: the
  // mail server refused the mail (424), the mail gateway was never reached
  // (502), it cannot send (503).
  for (const [doc, status, note] of [
    [{ error: 'send failed: SMTP send failed: (535, b"auth")', sent: false, request_key: 'KEY' }, 424,
     'Not sent: send failed: SMTP send failed: (535, b&quot;auth&quot;)'],
    [{ error: 'The mail gateway could not be reached (refused); nothing was sent.', sent: false }, 502,
     'Not sent: The mail gateway could not be reached (refused); nothing was sent.'],
    [{ error: 'the mail gateway is not configured', sent: false, request_key: 'KEY' }, 503, 'Not sent: the mail gateway is not configured'],
    [{ sent: false }, 502, 'Not sent: the server could not send it (502).'],
    [{}, 424, 'Not sent: the server refused it (424).'],
  ]) {
    const el = await open();
    let restored = '';
    el._restoreComposer = (t) => { restored = t; el._draft = t; };
    let req = await send(el, 'will it go?', doc, status);
    const key = req.body.request_key;
    assert.equal(el._notes.at(-1), note, String(status));
    assert.ok(!el._notes.some((n) => /could not confirm|not known/i.test(n)), el._notes.join(' | '));
    assert.equal(restored, 'will it go?', 'the words stay in the box');
    assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
    assert.equal(el._retry, null, 'the press is over: its key is dropped');
    // The mail server is back: the same words pressed again are a new
    // attempt under a new key — not the failed one's, which a gateway
    // would answer from its entry without trying.
    req = await send(el, 'will it go?', sentMsg('s-after', 'will it go?'));
    assert.match(req.body.request_key, KEY_RE);
    assert.notEqual(req.body.request_key, key, String(status));
    assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 's-after']);
  }
  // The same answers with the mail gateway named as the one that said so.
  for (const [doc, status] of [
    [{ error: 'send failed: refused', sent: false, request_key: 'KEY', by: 'gateway' }, 424],
    [{ error: 'no recipient', request_key: 'KEY', by: 'gateway' }, 400],
    [{ error: 'not configured', sent: false, request_key: 'KEY', by: 'gateway' }, 503],
  ]) {
    const el = await open();
    el._restoreComposer = (t) => { el._draft = t; };
    let req = await send(el, 'will it go?', doc, status);
    const key = req.body.request_key;
    assert.ok(el._notes.at(-1).startsWith('Not sent: '), String(status));
    assert.equal(el._retry, null, String(status));
    req = await send(el, 'will it go?', sentMsg('s-after', 'will it go?'));
    assert.notEqual(req.body.request_key, key, String(status));
  }
  // The dashboard's own "not sent" of a first press is "Not sent" too — but
  // only the mail gateway's word ends a key: this one stays with the words,
  // which costs nothing (the gateway never saw it, and takes it as new).
  for (const [doc, status] of [
    [{ error: 'The mail gateway could not be reached; nothing was sent.', sent: false, request_key: 'KEY', by: 'dashboard' }, 502],
    [{ error: 'no mail gateway serves that mailbox', sent: false, request_key: 'KEY', by: 'dashboard' }, 409],
  ]) {
    const el = await open();
    el._restoreComposer = (t) => { el._draft = t; };
    let req = await send(el, 'will it go?', doc, status);
    const key = req.body.request_key;
    assert.equal(el._notes.at(-1), `Not sent: ${doc.error}`, String(status));
    assert.equal(el._retry && el._retry.key, key, 'the key stays with the words');
    // Pressed again and refused by the dashboard again: it is known that
    // nothing went out under that key, so this is "Not sent" once more.
    req = await send(el, 'will it go?', doc, status);
    assert.equal(req.body.request_key, key);
    assert.equal(el._notes.at(-1), `Not sent: ${doc.error}`, String(status));
    assert.ok(!el._notes.some((n) => /not known/i.test(n)), el._notes.join(' | '));
    req = await send(el, 'will it go?', sentMsg('s-after', 'will it go?'));
    assert.equal(req.body.request_key, key, 'the gateway takes the key it never saw as a new mail');
    assert.equal(el._retry, null, 'answered: the key is spent');
  }
});

await ok('a press made again whose first outcome is unknown loses its key only on the mail gateway\'s word', async () => {
  // The first press got no answer (the stack restarted mid-send: the mail
  // may have left). Pressed again while the mail gateway is still starting,
  // the dashboard answers for itself — that request was never put to the
  // gateway, and nothing is learnt of the first. The key stays.
  for (const [doc, status] of [
    [{ error: 'The mail gateway could not be reached ([Errno 111] Connection refused); nothing was sent.',
       sent: false, request_key: 'KEY', by: 'dashboard' }, 502],
    [{ error: 'no mail gateway serves that mailbox', sent: false, request_key: 'KEY', by: 'dashboard' }, 409],
    [{ error: 'cross-site request rejected', by: 'dashboard' }, 403],
    [{ error: 'gone', by: 'dashboard' }, 410],
    // A server too old to say who answered, and a proxy's own 4xx.
    [{ error: 'The mail gateway could not be reached; nothing was sent.', sent: false, request_key: 'KEY' }, 502],
    [{ error: 'send failed: refused', sent: false, request_key: 'KEY' }, 424],
    [null, 413],
  ]) {
    const el = await open();
    el._restoreComposer = (t) => { el._draft = t; };
    let req = await send(el, 'twice', 'offline');
    const key = req.body.request_key;
    req = await send(el, 'twice', doc && doc.request_key ? { ...doc, request_key: key } : doc, status);
    assert.equal(req.body.request_key, key, 'the repeat went under the first key');
    const why = (doc && doc.error) || `it answered ${status}`;
    assert.equal(el._notes.at(-1), `The server could not be asked about this mail just now (${why}). `
      + 'It is still not known whether the first attempt went out. It is back in the box &mdash; '
      + 'if it shows up in the thread, do not send it again.', String(status));
    assert.ok(!el._notes.some((n) => n.startsWith('Not sent')), el._notes.join(' | '));
    assert.equal(el._draft, 'twice', 'the words are in the box');
    assert.equal(el._retry && el._retry.key, key, `${status}: the key stays with them`);
    assert.equal(el._retry.sure, false);
    // Asked once the gateway is back: the same key, answered as a repeat.
    req = await send(el, 'twice', sentMsg('s-twice', 'twice', { repeat: true }));
    assert.equal(req.body.request_key, key, String(status));
  }
  // The mail gateway's own word about the key ends the press.
  for (const [doc, status] of [
    [{ error: 'send failed: refused', sent: false, request_key: 'KEY', by: 'gateway' }, 424],
    [{ error: 'This thread has no mail left to reply to.', request_key: 'KEY', by: 'gateway' }, 409],
    [{ error: 'the mail gateway is not configured', sent: false, request_key: 'KEY', by: 'gateway' }, 503],
  ]) {
    const el = await open();
    el._restoreComposer = (t) => { el._draft = t; };
    let req = await send(el, 'twice', 'offline');
    const key = req.body.request_key;
    req = await send(el, 'twice', { ...doc, request_key: key }, status);
    assert.equal(req.body.request_key, key);
    assert.equal(el._notes.at(-1), `Not sent: ${doc.error}`, String(status));
    assert.equal(el._retry, null, String(status));
    req = await send(el, 'twice', sentMsg('s-twice', 'twice'));
    assert.notEqual(req.body.request_key, key, String(status));
  }
  // The key kept across a reload is still the unknown one's.
  realKeep = true;
  try {
    let el = await open();
    el._restoreComposer = (t) => { el._draft = t; el._keepLocal(); };
    let req = await send(el, 'across', 'offline');
    const key = req.body.request_key;
    req = await send(el, 'across', { error: 'unreached', sent: false, by: 'dashboard' }, 502);
    el = await open({}, BASE, [], PV(), true);
    assert.equal(el._draft, 'across');
    assert.equal(el._retry && el._retry.key, key);
    assert.equal(el._retry.sure, false);
    el._restoreComposer = (t) => { el._draft = t; };
    req = await send(el, 'across', { error: 'unreached', sent: false, by: 'dashboard' }, 502);
    assert.equal(req.body.request_key, key);
    assert.ok(el._notes.at(-1).startsWith('The server could not be asked about this mail just now'), el._notes.at(-1));
  } finally {
    realKeep = false;
  }
});

await ok('one key per press: other words in the box, even for a moment, end the kept press', async () => {
  // The connection dropped after the POST; the words are back with key K.
  const start = async () => {
    const el = await open();
    el._keepLocal = Page.prototype._keepLocal;
    el._restoreComposer = (t) => { el._draft = t; el._keepLocal(); };
    const req = await send(el, 'Thanks!', 'offline');
    assert.equal(el._retry.key, req.body.request_key);
    return { el, key: req.body.request_key };
  };
  const stored = () => JSON.parse(store.get(`retinue-draft:chat:${ID}`) || 'null');
  // The user empties the box — the mail did go out, they saw it — and later
  // answers Ann's next mail with the same short words: a new mail.
  let { el, key } = await start();
  assert.equal(stored().v.retry.key, key);
  el._draft = '';
  await settle();
  assert.equal(el._retry, null, 'the kept press went with its words');
  assert.ok(!stored() || !stored().v.retry, 'on the device too');
  el._draft = 'Thanks!';
  await poll(el, [...BASE, mail('m4', 'in', '2026-11-20T12:00:00Z')], []);
  let req = await send(el, 'Thanks!', sentMsg('s2', 'Thanks!'));
  assert.notEqual(req.body.request_key, key, 'the next mail has its own key');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'm4', 's2'], 'and it is sent');
  assert.ok(!el._notes.some((n) => n.includes('had gone out the first time')), el._notes.join(' | '));
  // Edited and put back as it was: other words were in the box in between.
  ({ el, key } = await start());
  el._draft = 'Thanks!!';
  await settle();
  el._draft = 'Thanks!';
  await settle();
  req = await send(el, 'Thanks!', sentMsg('s3', 'Thanks!'));
  assert.notEqual(req.body.request_key, key);
  // Words appended to it (Check again's refusal, a dictation), a draft
  // from elsewhere taking the box: the same.
  ({ el, key } = await start());
  el._intoComposer('more');
  await settle();
  assert.equal(el._retry, null);
  // Untouched, the press is the same press — the box emptied by the press
  // itself is no edit.
  ({ el, key } = await start());
  await settle();
  el._draft = '';
  req = await send(el, 'Thanks!', sentMsg('s4', 'Thanks!'));
  assert.equal(req.body.request_key, key, 'the same press again');
  // Across a reload: the device no longer holds the key either.
  ({ el, key } = await start());
  el._draft = 'Other words';
  await settle();
  el._keepLocal();
  el = await open({}, BASE, [], PV(), true);
  assert.equal(el._draft, 'Other words');
  assert.equal(el._retry, null);
});

await ok('a kept press whose mail shows up in the thread is over: the key goes, and the words leave the box', async () => {
  const now = () => new Date().toISOString();
  const start = async (text = 'did it go?') => {
    const el = await open();
    el._restoreComposer = (t) => { el._draft = t; };
    el._spent = [];
    el._saveDraft = () => { el._spent.push(el._draft); };
    el._undos = [];
    el._setUndo = (u) => { el._undos.push(u); };
    const req = await send(el, text, 'offline');
    assert.equal(el._draft, text);
    return { el, key: req.body.request_key };
  };
  // The mail did go out: a poll shows it, sent by the user, at the press.
  let { el, key } = await start();
  const went = mail('went@me.example.org', 'out', now(), { text: 'did it go?', author: 'user', sender: ACCT });
  await poll(el, [...BASE, went], []);
  assert.equal(el._retry, null, 'its outcome is known: the key is dropped');
  assert.equal(el._draft, '', 'the words are out of the box — one press would have sent them twice');
  assert.deepEqual(el._spent, [''], 'and out of the shared draft');
  assert.deepEqual(el._undos.at(-1), { text: 'did it go?', byAra: false }, 'offered back, as after a clear');
  assert.equal(el._notes.at(-1), 'That mail is in the thread after all, so it was taken out of the box &mdash; it is not sent twice.');
  // Typed again, the same words are a new mail.
  let req = await send(el, 'did it go?', sentMsg('s5', 'did it go?'));
  assert.notEqual(req.body.request_key, key);
  // Not a mail from before the press, not somebody else's, not other words.
  for (const other of [
    mail('old@me.example.org', 'out', new Date(Date.now() - 3600 * 1000).toISOString(), { text: 'did it go?', author: 'user', sender: ACCT }),
    mail('in@x.org', 'in', now(), { text: 'did it go?' }),
    mail('else@me.example.org', 'out', now(), { text: 'did it go? yes', author: 'user', sender: ACCT }),
  ]) {
    ({ el, key } = await start());
    await poll(el, [...BASE, other], []);
    assert.equal(el._retry.key, key, other.id);
    assert.equal(el._draft, 'did it go?', other.id);
  }
  // The server lists the send under the press's key (it waits for approval,
  // is being sent, failed): that entry is the press, whatever its words.
  for (const extra of [{}, { status: 'sending' }, { status: 'failed', outcome_unknown: true, error: 'No answer.' }]) {
    ({ el, key } = await start());
    await poll(el, BASE, [pend('q7', { text: 'did it go? (as the gateway trimmed it)', request_key: key, ...extra })]);
    assert.equal(el._retry, null, JSON.stringify(extra));
    assert.equal(el._draft, '', JSON.stringify(extra));
    assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q7']);
  }
  // But an entry that failed for certain is the server's word that the
  // press did NOT go (the gateway was stopped before the mail was handed
  // over): the press is over and its key with it, and the words stay in the
  // box — they were not sent, and the next press is a new mail.
  ({ el, key } = await start());
  await poll(el, BASE, [pend('q9', { text: 'did it go?', request_key: key, status: 'failed',
                                    error: 'the gateway was stopped before this mail was handed to the mail server; it was not sent <b>' })]);
  assert.equal(el._retry, null);
  assert.equal(el._draft, 'did it go?', 'nothing is taken out of the box');
  assert.deepEqual([el._spent, el._undos], [[], []]);
  assert.equal(el._notes.at(-1), 'Not sent: the gateway was stopped before this mail was handed to the mail server; ' +
    'it was not sent &lt;b&gt; The words are still in the box.');
  assert.ok(!el._notes.some((n) => n.includes('in the thread after all')), el._notes.join(' | '));
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q9'], 'the failed send says why, in the thread');
  req = await send(el, 'did it go?', sentMsg('s6', 'did it go?'));
  assert.notEqual(req.body.request_key, key, 'a new attempt, under a new key');
  // An entry the gateway only keeps to remember the key (`dismissed`) is
  // none: it settles nothing.
  ({ el, key } = await start());
  await poll(el, BASE, [pend('q10', { text: 'did it go?', request_key: key, status: 'failed', dismissed: true, error: 'x' })]);
  assert.equal(el._retry.key, key);
  assert.equal(el._draft, 'did it go?');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  // Another press's entry is not this one's.
  ({ el, key } = await start());
  await poll(el, BASE, [pend('q8', { text: 'something else', request_key: 'another-press-key-0000' })]);
  assert.equal(el._retry.key, key);
  // A poll that came back while the press was out showed the mail already:
  // the answer that never came does not leave the words armed in the box.
  el = await open();
  el._restoreComposer = (t) => { el._draft = t; };
  el._saveDraft = () => {};
  el._setUndo = () => {};
  const flying = el._sendChat('quick one');
  await settle();
  answerPreview(el, PV(), 'the preview at the press');
  await settle();
  const out = take((w) => w.method === 'POST', 'the send');
  await poll(el, [...BASE, mail('quick@me.example.org', 'out', now(), { text: 'quick one', author: 'user', sender: ACCT })], []);
  out.fail();
  await flying;
  await quiet(el);
  assert.equal(el._retry, null);
  assert.equal(el._draft, '');
  assert.equal(el._notes.at(-1), 'That mail is in the thread after all, so it was taken out of the box &mdash; it is not sent twice.');
  // The words were changed meanwhile: nothing is taken out of the box.
  ({ el, key } = await start());
  el._draft = 'did it go? — and one more thing';
  await poll(el, [...BASE, went], []);
  assert.equal(el._retry, null);
  assert.equal(el._draft, 'did it go? — and one more thing');
  assert.deepEqual(el._spent, []);
  // On a reload the chat is asked the same: the mail is there, the words
  // do not come back armed.
  el = await open();
  el._keepLocal = Page.prototype._keepLocal;
  el._restoreComposer = (t) => { el._draft = t; el._keepLocal(); };
  req = await send(el, 'kept, and sent', 'offline');
  assert.equal(JSON.parse(store.get(`retinue-draft:chat:${ID}`)).v.retry.key, req.body.request_key);
  const there = mail('there@me.example.org', 'out', now(), { text: 'kept, and sent', author: 'user', sender: ACCT });
  const spent = [];
  const realSave = Page.prototype._saveDraft;
  Page.prototype._saveDraft = function () { spent.push(this._draft); };
  try {
    el = await open({}, [...BASE, there], [], PV(), true);
  } finally {
    Page.prototype._saveDraft = realSave;
  }
  assert.equal(el._retry, null);
  assert.deepEqual(spent, [''], 'the box was emptied');
});

await ok('a dismissed entry is over: no bubble, and one on screen leaves', async () => {
  // The gateway keeps a dismissed send for a day so that its key stays
  // known, and answers for it by id as {status: error, dismissed: true}.
  // Whoever passes such an entry on, the page does not draw it as failed.
  const gone = pend('d1', { status: 'failed', dismissed: true, error: 'SMTP send failed: (535) auth', text: 'old words' });
  let el = await open({}, BASE, [gone, pend('q1')]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q1'], 'on a load');
  assert.deepEqual(el._pending.map((x) => x.p.request_id), ['q1']);
  await poll(el, BASE, [gone, pend('q1')]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q1'], 'on a poll');
  // A failed send on screen that the next answer marks dismissed (another
  // device dismissed it) leaves like one no longer listed.
  el = await open({}, BASE, [pend('d2', { status: 'failed', error: 'refused' })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:d2']);
  await poll(el, BASE, [pend('d2', { status: 'failed', error: 'refused', dismissed: true })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
  // Only the plain boolean is that word.
  await poll(el, BASE, [pend('d3', { status: 'failed', error: 'refused', dismissed: 'no' })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:d3']);
  // An unconfirmed mail of the page's own is not handed over to it either.
  el = await open();
  el._restoreComposer = (t) => { el._draft = t; };
  const p = el._sendChat('the mail');
  await settle();
  answerPreview(el, PV(), 'the preview at the press');
  await settle();
  const req = take((w) => w.method === 'POST', 'the send');
  req.answer({ unconfirmed: true, request_key: req.body.request_key }, 202);
  await p;
  await quiet(el);
  await poll(el, BASE, [pend('d4', { status: 'failed', dismissed: true, request_key: req.body.request_key, text: 'the mail' })]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1'], 'its own bubble stays, with its question');
});

await ok('a poll does not draw a second bubble for the press that is on its way', async () => {
  const el = await open();
  const p = el._sendChat('slow one');
  await settle();
  answerPreview(el, PV(), 'the preview at the press');
  await settle();
  const req = take((w) => w.method === 'POST', 'the send');
  const key = req.body.request_key;
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1']);
  // The gateway lists the mail while it is with the mail server.
  const flying = pend('f1', { status: 'sending', text: 'slow one', request_key: key });
  await poll(el, BASE, [flying, pend('q1')]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'local-1', 'P:q1'], 'one bubble for the press; other sends as ever');
  assert.deepEqual(el._pending.map((x) => x.p.request_id), ['q1']);
  // The mail server then refuses it: "Not sent", the words back — and no
  // failed bubble for words that are in the box.
  el._restoreComposer = (t) => { el._draft = t; };
  req.answer({ error: 'send failed: (535) auth', sent: false, request_key: key }, 424);
  await p;
  await quiet(el);
  assert.equal(el._notes.at(-1), 'Not sent: send failed: (535) auth');
  await poll(el, BASE, [pend('q1')]);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'P:q1']);
  // Sent: the mail takes the optimistic bubble's place, and a list that
  // still lags does not add the entry beside it.
  const b = await open();
  const pb = b._sendChat('slow two');
  await settle();
  answerPreview(b, PV(), 'the preview at the press');
  await settle();
  const rb = take((w) => w.method === 'POST', 'the send');
  const lag = pend('f2', { status: 'sending', text: 'slow two', request_key: rb.body.request_key, message_id: 's-two' });
  await poll(b, BASE, [lag]);
  rb.answer(sentMsg('s-two', 'slow two'));
  await pb;
  await quiet(b);
  await poll(b, [...BASE, sentMsg('s-two', 'slow two')], [lag]);
  assert.deepEqual(b._thread.layout(), ['m1', 'm2', 'm3', 's-two']);
  // Unconfirmed: once the answer is in, the server's entry takes over.
  const c = await open();
  const pc = c._sendChat('slow three');
  await settle();
  answerPreview(c, PV(), 'the preview at the press');
  await settle();
  const rc = take((w) => w.method === 'POST', 'the send');
  const mine = pend('f3', { status: 'sending', text: 'slow three', request_key: rc.body.request_key });
  await poll(c, BASE, [mine]);
  assert.deepEqual(c._thread.layout(), ['m1', 'm2', 'm3', 'local-1']);
  rc.answer({ unconfirmed: true, request_key: rc.body.request_key }, 202);
  await pc;
  await quiet(c);
  await poll(c, BASE, [mine]);
  assert.deepEqual(c._thread.layout(), ['m1', 'm2', 'm3', 'P:f3']);
});

await ok('a mail that never left the page is simply not sent', async () => {
  // An attachment that cannot be read: nothing was posted, so nothing can
  // have gone out — said plainly, with the words and the file back.
  const el = await open();
  const file = { blob: { size: 3 }, content_type: 'application/pdf', name: 'a.pdf', url: 'blob:x' };
  el._outImages = [file];
  el._imagePayload = () => Promise.reject(new Error('read'));
  el._keepImages = () => {};
  wire.length = 0;
  await el._sendChat('with a file');
  assert.equal(wire.filter((w) => w.method === 'POST').length, 0);
  assert.equal(el._notes.at(-1), 'Not sent: an attachment could not be read.');
  assert.deepEqual(el._outImages, [file]);
  assert.equal(el._draft, 'with a file');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3']);
});

await ok('the key stays with the words across a reload', async () => {
  // The connection dropped; the page keeps the press with the words.
  let el = await open();
  el._keepLocal = Page.prototype._keepLocal;
  el._restoreComposer = (t) => { el._draft = t; el._keepLocal(); };
  el._setReplyMode('sender');
  let req = await send(el, 'kept words', 'offline');
  const key = req.body.request_key;
  const kept = JSON.parse(store.get(`retinue-draft:chat:${ID}`)).v;
  assert.ok(Math.abs(Date.now() - kept.retry.at) < 60000);
  assert.deepEqual({ ...kept.retry, at: 0 }, { key, text: 'kept words', mode: 'sender', imgs: '[]', at: 0, seen: 0, sure: false });
  // The words reach the server's draft; the press is kept all the same.
  el._draftSaved = 'kept words';
  el._keepLocal();
  assert.equal(JSON.parse(store.get(`retinue-draft:chat:${ID}`)).v.retry.key, key);
  // Reloaded: the same words, the same choice, the same key.
  el = await open({ draft: { text: 'kept words', version: 3, author: 'user' } }, BASE, [], PV(), true);
  assert.equal(el._draft, 'kept words');
  assert.equal(el._replyMode, 'sender');
  req = await send(el, 'kept words', sentMsg('s1', 'kept words'));
  assert.equal(req.body.request_key, key);
  assert.equal(req.body.reply, 'sender');
  // A page left while the send was on the wire: the same.
  el = await open();
  el._keepLocal = Page.prototype._keepLocal;
  const p = el._sendChat('left mid-send');
  await settle();
  answerPreview(el, PV(), 'the preview at the press');
  await settle();
  const flying = take((w) => w.method === 'POST' && w.url === SEND_URL, 'the send');
  el._leaving();
  const left = JSON.parse(store.get(`retinue-draft:chat:${ID}`)).v.sending;
  assert.equal(left.key, flying.body.request_key);
  assert.equal(left.reply, 'all');
  el = await open({}, BASE, [], PV(), true);
  assert.equal(el._draft, 'left mid-send');
  assert.equal(el._leftSend, true);
  req = await send(el, 'left mid-send', sentMsg('s2', 'left mid-send'));
  assert.equal(req.body.request_key, flying.body.request_key, 'sent again, it is the mail that was on the wire');
  flying.fail();
  await p;
  // What is kept is read as data: a key that is no key is not used; nor is
  // one from long ago — the same words typed then are a mail of their own,
  // which the gateway must not take for the old one.
  const keep = (retry) => store.set(`retinue-draft:chat:${ID}`, JSON.stringify({ at: Date.now(), v: { text: 'x', version: 0, retry } }));
  const good = { key: 'k'.repeat(32), text: 'x', mode: 'all', imgs: '[]', at: Date.now() - 60 * 1000, seen: 0, sure: false };
  keep(good);
  el = await open({}, BASE, [], PV(), true);
  assert.deepEqual(el._retry, good);
  // A press is kept with its words only: where the box comes back holding
  // others — the server's draft moved on since — it is a press no longer.
  keep(good);
  el = await open({ draft: { text: 'written elsewhere since', version: 9, author: 'user' } }, BASE, [], PV(), true);
  assert.equal(el._draft, 'written elsewhere since');
  assert.equal(el._retry, null, 'the kept press is not carried to other words');
  for (const bad of [{ ...good, key: '../../etc' }, { ...good, text: 7 }, { ...good, at: Date.now() - 13 * 3600 * 1000 },
                     { ...good, at: 'now' }, { ...good, at: Date.now() + 3600 * 1000 }, 'k', null]) {
    keep(bad);
    el = await open({}, BASE, [], PV(), true);
    assert.equal(el._retry, null, JSON.stringify(bad));
  }
  keep(good);
  el = await open({}, BASE, [], PV(), true);
  el._retry.at = Date.now() - 13 * 3600 * 1000;
  req = await send(el, 'x', sentMsg('s3', 'x'));
  assert.notEqual(req.body.request_key, good.key, 'a press from long ago is not repeated');
});

await ok('a press made again whose first mail had gone out says so, once', async () => {
  const el = await open();
  el._restoreComposer = () => {};
  let req = await send(el, 'did it go?', 'offline');
  const key = req.body.request_key;
  // It did: the mail shows in the thread.
  const first = mail('first@me.example.org', 'out', '2026-11-20T10:00:05Z', { text: 'did it go?', author: 'user', sender: ACCT });
  await poll(el, [...BASE, first], []);
  // The words were back in the box, and are sent again all the same: the
  // gateway knows the press and answers with the mail it already sent.
  req = await send(el, 'did it go?', first);
  assert.equal(req.body.request_key, key);
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'first@me.example.org'], 'one mail, one bubble');
  assert.equal(el._notes.at(-1), 'This mail had gone out the first time; it was not sent again.');
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
  let put = '';
  el._companionEl = () => ({ ask: (t) => { put = t; } });
  el._setPane = () => {};
  el._quickPattern('translate');
  assert.equal(put, by('translate').prompt('', true));
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
  store.clear();
  el._startNew(0);
  assert.ok(sheetEl, 'the sheet is on the page');
  assert.deepEqual(sheetEl._offer, { channel: 'email', label: 'E-mail', accounts: [ACCT, 'work@example.com'] });
  assert.equal(sheetEl.hasAttribute('hidden'), false);
  sheetEl.close();
  assert.equal(back, 1, 'focus back on the ＋');
});

await ok('the mailboxes on offer are asked for again as the ＋ is opened', async () => {
  // The work mailbox was away when the page loaded.
  const el = await mountList({ channels: [{ channel: 'email', label: 'E-mail', accounts: [ACCT] }] });
  const offer = el._compose[0];
  let p = el._refreshCompose();
  el._refreshCompose();
  assert.equal(wire.filter((w) => w.url === '/chats/compose').length, 1, 'one question at a time');
  take((w) => w.url === '/chats/compose', 'GET /chats/compose')
    .answer({ channels: [{ channel: 'email', label: 'E-mail', accounts: [ACCT, 'work@example.com'] }] });
  await p;
  assert.equal(el._compose[0], offer, 'the offer on the menu is the same entry');
  assert.deepEqual(offer.accounts, [ACCT, 'work@example.com'], 'with the mailbox that came back');
  // A failed question changes nothing.
  for (const status of [502, 404]) {
    p = el._refreshCompose();
    take((w) => w.url === '/chats/compose', 'GET /chats/compose').answer({}, status);
    await p;
    assert.deepEqual(el._compose[0].accounts, [ACCT, 'work@example.com']);
  }
  p = el._refreshCompose();
  take((w) => w.url === '/chats/compose', 'GET /chats/compose').fail();
  await p;
  assert.equal(el._compose.length, 1);
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
  assert.ok(m.includes('title="Moves the thread&#39;s mails to the Trash of me@example.org, then erases the chat here"'), m);
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

await ok('a chat that is only a queued mail previews as waiting — or as what became of it', async () => {
  const last = (extra = {}) => listChat('k', { last: { ts: 't', direction: 'out', author: 'user', text: 'Hi <Zoe>', pending: true, ...extra } });
  assert.equal(previewHtml(last()), '<span class="pv-who">You:</span> <span class="pv-pending">Waiting for approval:</span> Hi &lt;Zoe&gt;');
  assert.ok(previewHtml(last({ status: 'sending' })).includes('<span class="pv-pending">Sending:</span>'));
  assert.ok(previewHtml(last({ status: 'failed' })).includes('<span class="pv-pending">Not sent:</span>'));
  assert.ok(previewHtml(last({ status: 'failed', outcome_unknown: true })).includes('<span class="pv-pending">Not confirmed:</span>'));
  assert.ok(previewHtml(last({ status: '<b>' })).includes('Waiting for approval:'));
});

// ── The compose sheet ────────────────────────────────────────────────────────
await ok('addresses by shape, split as a header would be', async () => {
  for (const a of ['ann@example.com', 'a.b+tag@sub.example.co', 'x@y.museum', "o'neil@x.org",
                   'x@xn--exmple-cua.de']) assert.ok(isAddress(a), a);
  // What the mail gateway would refuse at Send is not taken here either: an
  // address with letters outside ASCII (no SMTPUTF8), an encoded word, a
  // local part over 64 characters.
  for (const a of ['jörg@example.de', 'joerg@exämple.de', '=?utf-8?q?x?=@x.org',
                   `${'a'.repeat(65)}@x.org`, 'a..b@x.org', '.a@x.org', 'a@-x.org']) assert.ok(!isAddress(a), a);
  for (const a of ['ann', 'ann@', '@x.org', 'ann@x', 'a b@x.org', 'a@x.o', 'a@@x.org', 'a<b>@x.org', 'a,b@x.org',
                   'a@x..org', `${'a'.repeat(250)}@x.org`, null, 3]) assert.ok(!isAddress(a), String(a));
  const r = splitAddresses('"Doe, Jane" <jane@x.org>, b@y.org; Bob <bob@builder.example>\nlast');
  assert.deepEqual(r.tokens.map((t) => [t.address, t.name]),
                   [['jane@x.org', 'Doe, Jane'], ['b@y.org', ''], ['bob@builder.example', 'Bob'], ['last', '']]);
  assert.equal(r.tail, true);
  assert.equal(splitAddresses('a@x.org,').tail, false);
  assert.deepEqual(splitAddresses(' , ;').tokens, []);
});

const ONE = { channel: 'email', label: 'E-mail', accounts: [ACCT] };
const TWO = { channel: 'email', label: 'E-mail', accounts: [ACCT, 'work@example.com'] };
const SLOT = (account = ACCT) => `retinue-draft:compose:email:${account}`;
// `keep`: what the device holds stays (the sheet opened again, a reload).
function openSheet(offer = ONE, keep = false) {
  if (!keep) store.clear();
  assigned.length = 0;
  wire.length = 0;
  let back = 0;
  if (sheetEl && !keep) { sheetEl._busy = false; }
  const el = sheetMod.openComposeSheet(offer, () => { back += 1; });
  return { el, root: el.shadowRoot, back: () => back };
}
const q = (root, s) => root.querySelector(s);
const type = (root, f, value) => { const i = q(root, `[data-input="${f}"]`); i.value = value; root.fire('input', i); return i; };
const write = (root, to = 'ann@example.com,', text = 'Hello') => {
  type(root, 'to', to);
  const t = q(root, '[data-text]'); t.value = text; root.fire('input', t);
};
const kept = (account = ACCT) => (store.has(SLOT(account)) ? JSON.parse(store.get(SLOT(account))).v : null);

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
  const two = openSheet(TWO);
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
  // An emptied field has nothing left to be wrong about, at once.
  type(root, 'to', '');
  assert.equal(q(root, '[data-err="to"]').hidden, true, 'a clean field clears its error');
  root.fire('focusout', q(root, '[data-input="to"]'));
  assert.equal(q(root, '[data-err="to"]').hidden, true);
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

await ok('the recipient cap names what it kept out, keeps it in the field, and lets go again', async () => {
  const { el, root } = openSheet();
  const many = Array.from({ length: 60 }, (_, n) => `a${n}@x.org`);
  let i = type(root, 'to', `${many.join(', ')},`);
  assert.equal(el._chips.to.length, 50);
  assert.equal(i.value, many.slice(50).join(', '), 'the ten that did not fit are still in the field');
  assert.equal(q(root, '[data-err="to"]').textContent, `At most 50 recipients here; not added: ${many.slice(50).join(', ')}.`);
  // With them still there, a send is held back, and says why.
  write(root, i.value, 'Hello');
  await el._submit();
  assert.equal(wire.filter((w) => w.method === 'POST').length, 0);
  assert.ok(q(root, '[data-err="to"]').textContent.startsWith('At most 50 recipients here; not added: a50@x.org'));
  // Two chips taken out: two of the waiting ones go in with the next commit.
  el._removeChip('to', 0);
  assert.equal(q(root, '[data-err="to"]').hidden, true, 'the cap\'s line goes with the chip');
  el._removeChip('to', 0);
  root.fire('focusout', q(root, '[data-input="to"]'));
  assert.equal(el._chips.to.length, 50);
  assert.deepEqual(el._chips.to.slice(-2).map((r) => r.address), ['a50@x.org', 'a51@x.org']);
  assert.equal(q(root, '[data-input="to"]').value, many.slice(52).join(', '));
  // The rest deleted from the field: nothing stands in the way, from the
  // keyboard alone.
  type(root, 'to', '');
  assert.equal(q(root, '[data-err="to"]').hidden, true);
  let p = el._submit();
  let req = take((w) => w.method === 'POST', 'the compose');
  assert.equal(req.body.to.length, 50);
  req.answer({ error: 'no' }, 400);
  await p;
  // A full field and a pick from the address book: said, and Backspace —
  // the keyboard's way to take a chip back — clears it.
  el._sugg = { field: 'to', items: [{ name: 'Zed', address: 'zed@x.org' }], active: 0 };
  el._pick('to', 0);
  assert.equal(el._chips.to.length, 50);
  assert.equal(q(root, '[data-err="to"]').textContent, 'At most 50 recipients here; not added: zed@x.org.');
  i = q(root, '[data-input="to"]');
  root.fire('keydown', i, { key: 'Backspace' });
  assert.equal(el._chips.to.length, 49);
  assert.equal(q(root, '[data-err="to"]').hidden, true);
  p = el._submit();
  req = take((w) => w.method === 'POST', 'the compose');
  assert.equal(req.body.to.length, 49, 'Send is not blocked by a line nothing could clear');
  req.answer({ error: 'no' }, 400);
  await p;
  // What is no address and what did not fit are both named.
  const full = openSheet();
  type(full.root, 'to', `${many.slice(0, 50).join(', ')}, nope, late@x.org,`);
  assert.equal(q(full.root, '[data-err="to"]').textContent,
               '“nope” is not an e-mail address. At most 50 recipients here; not added: late@x.org.');
  assert.equal(q(full.root, '[data-input="to"]').value, 'nope, late@x.org');
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

await ok('an address-book answer for a field that was left opens no list', async () => {
  const BOOK = { contacts: [{ name: 'Ann Example', handles: [{ channel: 'email', handle: 'ann@example.com' }] }] };
  // Typed "ann", then on to the subject before the answer is back.
  let { el, root } = openSheet();
  let i = type(root, 'to', 'ann');
  let p = el._suggest('to', 'ann');
  let req = take((w) => w.url.startsWith('/contacts?'), 'the address book');
  q(root, '[data-subject]').focus();
  root.fire('focusout', i);
  assert.equal(i.value, 'ann', 'what is no address stays in the field');
  assert.equal(q(root, '[data-err="to"]').textContent, '“ann” is not an e-mail address.');
  req.answer(BOOK);
  await p;
  assert.deepEqual(el._sugg, { field: '', items: [], active: -1 }, 'nothing opens under a field nobody is in');
  assert.equal(q(root, '[data-sugg="to"]').hidden, true);
  // The same with the question still waiting for its pause: it is not asked.
  ({ el, root } = openSheet());
  i = type(root, 'to', 'ann');
  el._suggTimer = 7;
  root.fire('focusout', i);
  assert.equal(el._suggTimer, null);
  // And however the field was left: an answer only opens a list under the
  // field that has the focus.
  ({ el, root } = openSheet());
  i = type(root, 'to', 'ann');
  p = el._suggest('to', 'ann');
  req = take((w) => w.url.startsWith('/contacts?'), 'the address book');
  q(root, '[data-text]').focus();
  req.answer(BOOK);
  await p;
  assert.deepEqual(el._sugg.items, []);
  // Escape in the subject then closes the sheet — there is no list in its way.
  root.fire('keydown', q(root, '[data-subject]'), { key: 'Escape' });
  assert.equal(el.hasAttribute('hidden'), true);
});

await ok('an input method\'s Enter only commits what it composes', async () => {
  const { el, root } = openSheet();
  // WebKit: the Enter that commits a composition arrives after
  // compositionend — isComposing false, keyCode 229.
  for (const ime of [{ isComposing: true }, { isComposing: false, keyCode: 229 }]) {
    const i = type(root, 'to', '山田');
    el._sugg = { field: 'to', items: [{ name: '山田', address: 'yamada@x.org' }], active: -1 };
    let ev = root.fire('keydown', i, { key: 'Enter', ...ime });
    assert.ok(ev.defaultPrevented, 'never a submit');
    assert.deepEqual(el._chips.to, [], 'no chip is made of a name being composed');
    assert.equal(q(root, '[data-err="to"]').hidden, true, 'and no error about it');
    assert.equal(el._sugg.items.length, 1, 'the list it was typed for stays');
    // In the subject it does not jump on to the text.
    const subj = q(root, '[data-subject]');
    subj.focus();
    ev = root.fire('keydown', subj, { key: 'Enter', ...ime });
    assert.ok(ev.defaultPrevented);
    assert.equal(globalThis.focused, subj);
    // In the text it does not send.
    write(root, 'ann@example.com,', 'Hello');
    root.fire('keydown', q(root, '[data-text]'), { key: 'Enter', ctrlKey: true, ...ime });
    await settle();
    assert.equal(wire.filter((w) => w.method === 'POST').length, 0);
    el._discard();
  }
  // A plain Enter still does all three.
  const i = type(root, 'to', 'zoe@x.org');
  root.fire('keydown', i, { key: 'Enter', keyCode: 13 });
  assert.deepEqual(el._chips.to.map((r) => r.address), ['zoe@x.org']);
  const subj = q(root, '[data-subject]');
  root.fire('keydown', subj, { key: 'Enter', keyCode: 13 });
  assert.equal(globalThis.focused, q(root, '[data-text]'));
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

await ok('a send: the request, then the new chat — sent, queued, or handed over', async () => {
  for (const [status, answer] of [[200, { id: 'email:~me@example.org:new1@me.example.org', message: { id: 'new1@me.example.org' } }],
                                  [202, { id: 'email:~me@example.org:new2@me.example.org', pending: pend('q7') }],
                                  [202, { id: 'email:~me@example.org:new3@me.example.org', unconfirmed: true, message_id: 'new3@me.example.org' }]]) {
    const { el, root } = openSheet(TWO);
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
    assert.match(req.body.request_key, KEY_RE);
    assert.deepEqual({ ...req.body, request_key: 0 },
                     { channel: 'email', to: ['ann@example.com', 'bob@builder.example'], cc: ['carla@example.net'],
                       subject: 'Numbers', text: 'Hi both,\n\nthe numbers.', account: 'work@example.com', request_key: 0 });
    assert.equal(q(root, '[data-send]').textContent, 'Sending …');
    assert.equal(q(root, '[data-send]').disabled, true);
    assert.equal(q(root, '[data-discard]').disabled, true);
    el._submit();
    assert.equal(wire.filter((w) => w.method === 'POST').length, 0, 'one press, one request');
    req.answer(answer, status);
    await settle();
    await settle();
    assert.deepEqual(assigned, [`/chat.html?id=${encodeURIComponent(answer.id)}`]);
    assert.equal(kept('work@example.com'), null, 'the kept mail is dropped');
    assert.equal(q(root, '[data-send]').disabled, true, 'no second press on the way out');
    el._submit();
    assert.equal(wire.filter((w) => w.method === 'POST').length, 0);
  }
});

await ok('one press, one mail: closing and opening the sheet does not arm Send again', async () => {
  const { el, root } = openSheet();
  write(root, 'ann@example.com,', 'Slow mail');
  const p = el._submit();
  const first = take((w) => w.method === 'POST', 'the compose');
  // Nothing seems to happen: Escape, then ＋ → New e-mail again.
  root.fire('keydown', q(root, '[data-text]'), { key: 'Escape' });
  assert.equal(el.hasAttribute('hidden'), true, 'closing stays possible');
  const again = sheetMod.openComposeSheet(ONE, () => {});
  assert.equal(again, el);
  assert.equal(el.hasAttribute('hidden'), false);
  assert.ok(el.shadowRoot.innerHTML.includes('This mail is being sent.'), 'said');
  assert.ok(!el.shadowRoot.innerHTML.includes('The mail you started earlier is back.'), 'not offered as a kept draft');
  assert.deepEqual(el._chips.to.map((r) => r.address), ['ann@example.com']);
  assert.equal(el._text, 'Slow mail');
  assert.equal(q(el.shadowRoot, '[data-send]').disabled, true);
  assert.equal(q(el.shadowRoot, '[data-send]').textContent, 'Sending …');
  await el._submit();
  root.fire('keydown', q(el.shadowRoot, '[data-text]'), { key: 'Enter', ctrlKey: true });
  el._discard();
  assert.equal(el._text, 'Slow mail', 'the mail on its way is not emptied');
  assert.equal(wire.filter((w) => w.method === 'POST').length, 0, 'no second request while the first is on its way');
  first.answer({ id: 'email:~me@example.org:slow@me.example.org', message: {} });
  await p;
  assert.deepEqual(assigned, [`/chat.html?id=${encodeURIComponent('email:~me@example.org:slow@me.example.org')}`]);
  // A send that fails after the sheet was closed is not left unsaid: the
  // sheet comes back with what happened, and everything still there.
  const b = openSheet();
  write(b.root, 'ann@example.com,', 'Slow mail');
  const pb = b.el._submit();
  const req = take((w) => w.method === 'POST', 'the compose');
  b.el.close();
  req.answer({ error: 'The address does not exist.' }, 400);
  await pb;
  assert.equal(b.el.hasAttribute('hidden'), false);
  assert.equal(q(b.el.shadowRoot, '[data-err="form"]').textContent, 'Not sent: The address does not exist.');
  assert.equal(q(b.el.shadowRoot, '[data-send]').disabled, false);
  assert.equal(b.el._text, 'Slow mail');
  assert.equal(main.inert, true);
  b.el.close();
});

await ok('a refusal is shown in place; an answer that leaves it open is never "not sent"', async () => {
  const cases = [
    [{ error: 'The address "x@y.zz" does not parse.' }, 400, 'Not sent: The address "x@y.zz" does not parse.'],
    [{}, 403, 'Not sent: the server refused the request.'],
    [{}, 409, 'Not sent: the server refused the request (409).'],
    [{}, 503, 'The server could not confirm this mail (it answered 503). If it shows up in the chat list, do not send it again.'],
    [{ error: 'gateway unreachable' }, 502, 'The server could not confirm this mail (gateway unreachable). If it shows up in the chat list, do not send it again.'],
    // The server's word that nothing went out, whatever its status: the mail
    // server refused the mail (424), the mail gateway was never reached
    // (502), it cannot send (503). Only the plain boolean is that word.
    [{ error: 'send failed: SMTP send failed: (451) try later', sent: false, request_key: 'K' }, 424,
     'Not sent: send failed: SMTP send failed: (451) try later'],
    [{ sent: false }, 424, 'Not sent: the server refused the request (424).'],
    [{ error: 'The mail gateway could not be reached; nothing was sent.', sent: false }, 502,
     'Not sent: The mail gateway could not be reached; nothing was sent.'],
    [{ error: 'the mail gateway is not configured', sent: false, request_key: 'K' }, 503, 'Not sent: the mail gateway is not configured'],
    [{ sent: false }, 502, 'Not sent: the server could not send it (502).'],
    [{ error: 'x', sent: 'false' }, 502, 'The server could not confirm this mail (x). If it shows up in the chat list, do not send it again.'],
    [{ error: 'x', sent: 0 }, 503, 'The server could not confirm this mail (x). If it shows up in the chat list, do not send it again.'],
    [{ message: {} }, 200, 'The server did not say where the new chat is — look for it in the chat list.'],
    [{ unconfirmed: true }, 202, 'The mail was handed over, but it is not confirmed yet that it went out. Look for it in the chat list before you send it again.'],
    [null, 0, 'No answer came back, so it is not known whether the mail went out. If it shows up in the chat list, do not send it again.'],
  ];
  for (const [doc, status, said] of cases) {
    const { el, root } = openSheet();
    write(root);
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
    // Only the server's word that nothing went out — its own refusal, or
    // an answer that says "sent": false — reads "not sent"; then, and only
    // then, the press is over and the next one is a new one. A failed key
    // pressed again would be answered from the failure without a try.
    const open = !((status >= 400 && status < 500) || (!!doc && doc.sent === false));
    assert.equal(/^Not sent/.test(said), !open, said);
    assert.equal(kept().press === null, !open, `${status}: the press kept with the mail`);
    const p2 = el._submit();
    const req2 = take((w) => w.method === 'POST', 'the compose again');
    assert.equal(req2.body.request_key === req.body.request_key, open, `${status}: the same press again keeps its key`);
    req2.answer({ error: 'x' }, 400);
    await p2;
  }
  // The server echoed the key: it knows this very press, and says so.
  const { el, root } = openSheet();
  write(root);
  let p = el._submit();
  let req = take((w) => w.method === 'POST', 'the compose');
  const key = req.body.request_key;
  req.answer({ unconfirmed: true, request_key: key }, 202);
  await p;
  assert.equal(q(root, '[data-err="form"]').textContent, 'The mail was handed over, but it is not confirmed yet that it went out. ' +
    'Look for it in the chat list; sending it again unchanged asks about this same mail and does not send it twice.');
  // Unchanged: the same key. Changed: another mail, another key.
  p = el._submit();
  req = take((w) => w.method === 'POST', 'the compose');
  assert.equal(req.body.request_key, key);
  req.fail();
  await p;
  const text = q(root, '[data-text]'); text.value = 'Hello, edited'; root.fire('input', text);
  p = el._submit();
  req = take((w) => w.method === 'POST', 'the compose');
  assert.notEqual(req.body.request_key, key);
  req.answer({ id: 'email:~me@example.org:n@me.example.org', message: {} });
  await p;
});

await ok('a press that was never answered stays with the mail across a reload', async () => {
  const { el, root } = openSheet();
  write(root, 'ann@example.com,', 'On the wire');
  el._submit();
  const first = take((w) => w.method === 'POST', 'the compose');
  assert.equal(kept().press.key, first.body.request_key, 'kept with its press before the request left');
  // The page is reloaded while the request is on its way.
  el._busy = false;
  const { el: el2, root: root2 } = openSheet(ONE, true);
  assert.ok(root2.innerHTML.includes('The mail you started earlier is back. It was being sent when this sheet was last open, ' +
    'and it is not confirmed whether it went out: look in the chat list before you send it again.'), root2.innerHTML.slice(0, 900));
  assert.equal(q(root2, '[data-send]').disabled, false);
  const p = el2._submit();
  const req = take((w) => w.method === 'POST', 'the compose');
  assert.equal(req.body.request_key, first.body.request_key, 'sent again unchanged, it is the same mail to the gateway');
  req.answer({ id: 'email:~me@example.org:w@me.example.org', message: {} });
  await p;
  assert.equal(kept(), null);
  // What is kept is read as data.
  store.set(SLOT(), JSON.stringify({ at: Date.now(), v: { to: [{ address: 'ann@example.com' }], text: 'x', press: { key: '<script>', sig: 1 } } }));
  const c = openSheet(ONE, true);
  assert.equal(c.el._press, null);
  assert.ok(!c.root.innerHTML.includes('It was being sent'));
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
  sheetMod.openComposeSheet(ONE, () => { back2 += 1; });
  assert.deepEqual(el._chips.to.map((r) => r.address), ['ann@example.com']);
  assert.equal(el._subject, 'Later');
  assert.ok(el.shadowRoot.innerHTML.includes('The mail you started earlier is back.</p>'));
  assert.ok(el.shadowRoot.innerHTML.includes('value="Later"'));
  // Discard empties it for good.
  el._discard();
  assert.equal(kept(), null);
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
  el.close();
});

await ok('a kept mail belongs to its mailbox and never changes sender unseen', async () => {
  const WORK = 'work@example.com';
  const PRIV = 'me@example.org';
  const pick = (root, account) => { const a = q(root, '[data-account]'); a.value = account; root.fire('change', a); };
  // Started from the private mailbox, closed.
  let { el, root } = openSheet({ ...TWO, accounts: [WORK, PRIV] });
  assert.equal(el._account, WORK);
  pick(root, PRIV);
  write(root, 'ann@example.com,', 'A private matter');
  el.close();
  assert.equal(kept(PRIV).text, 'A private matter');
  assert.equal(kept(WORK), null);
  // Later the private mailbox cannot send and is not offered: the sheet does
  // not bring the mail back under the work address — it opens empty, says
  // where the mail is, and a send from here is another mail.
  ({ el, root } = openSheet({ ...ONE, accounts: [WORK] }, true));
  assert.equal(el._account, WORK);
  assert.deepEqual([el._chips.to, el._text], [[], '']);
  assert.ok(root.innerHTML.includes('<p class="from">From <b>work@example.com</b></p>'));
  assert.ok(!root.innerHTML.includes('The mail you started earlier is back.'));
  assert.ok(root.innerHTML.includes('A mail you started from me@example.org is kept. It comes back when that mailbox can send again.'));
  write(root, 'bob@builder.example,', 'Work');
  const p = el._submit();
  const req = take((w) => w.method === 'POST', 'the compose');
  assert.equal(req.body.account, WORK);
  assert.equal(req.body.text, 'Work');
  req.answer({ error: 'no' }, 400);
  await p;
  el.close();
  assert.equal(kept(PRIV).text, 'A private matter', 'untouched');
  assert.equal(kept(WORK).text, 'Work');
  // Both offered again: the sheet opens on the mailbox written from last,
  // with its own mail; the other mailbox's mail is under that mailbox.
  ({ el, root } = openSheet({ ...TWO, accounts: [WORK, PRIV] }, true));
  assert.equal(el._account, WORK);
  assert.equal(el._text, 'Work');
  assert.ok(root.innerHTML.includes(`<option value="${WORK}" selected>`));
  // From changed with both holding a mail: the chosen mailbox's own comes
  // back, the other stays kept — neither is lost, neither changes sender.
  pick(root, PRIV);
  assert.equal(el._account, PRIV);
  assert.equal(el._text, 'A private matter');
  assert.ok(el.shadowRoot.innerHTML.includes('The mail you started earlier is back. What you wrote from work@example.com is kept for that mailbox.'));
  assert.equal(kept(WORK).text, 'Work');
  // An empty form simply opens on what the chosen mailbox holds …
  el._discard();
  assert.equal(kept(PRIV), null);
  pick(el.shadowRoot, WORK);
  assert.equal(el._text, 'Work');
  // … and a mail goes with From only where the user sends it: to a mailbox
  // that holds none.
  pick(el.shadowRoot, PRIV);
  assert.equal(el._text, 'Work', 'the mail moved by the user\'s own choice');
  assert.equal(kept(WORK), null);
  assert.equal(kept(PRIV).text, 'Work');
  assert.equal(kept(PRIV).account, PRIV);
  const p2 = el._submit();
  const req2 = take((w) => w.method === 'POST', 'the compose');
  assert.equal(req2.body.account, PRIV);
  req2.answer({ id: 'email:~me@example.org:x@me.example.org', message: {} });
  await p2;
  // A mailbox that is not on offer is never posted.
  ({ el, root } = openSheet(ONE));
  write(root);
  el._account = 'gone@example.org';
  await el._submit();
  assert.equal(wire.filter((w) => w.method === 'POST').length, 0);
  assert.equal(q(root, '[data-err="form"]').textContent, 'Choose the mailbox this mail leaves from.');
});

await ok('a mail kept for another mailbox is not forgotten once a different one is written from', async () => {
  const WORK = 'work@example.com';
  const PRIV = 'me@example.org';
  const BOTH = { ...TWO, accounts: [WORK, PRIV] };
  const ONLY_WORK = { ...ONE, accounts: [WORK] };
  const INDEX = 'retinue-draft:compose:email';
  const index = () => (store.has(INDEX) ? JSON.parse(store.get(INDEX)).v : null);
  const pick = (root, account) => { const a = q(root, '[data-account]'); a.value = account; root.fire('change', a); };
  const WAITS = 'A mail you started from me@example.org is kept. It comes back when that mailbox can send again.';
  const THERE = 'A mail you started from me@example.org is kept. Choose that mailbox under From to go on with it.';
  // A mail started from the private mailbox; then that mailbox is away.
  const parkPrivate = () => {
    const { el, root } = openSheet(BOTH);
    pick(root, PRIV);
    write(root, 'ann@example.com,', 'A private matter');
    el.close();
    const away = openSheet(ONLY_WORK, true);
    assert.ok(away.root.innerHTML.includes(WAITS));
    return away;
  };
  // A work mail is written and SENT meanwhile. The private mailbox is still
  // away: the sheet still says that its mail waits …
  let { el, root } = parkPrivate();
  write(root, 'bob@builder.example,', 'Work');
  let p = el._submit();
  take((w) => w.method === 'POST', 'the compose').answer({ id: 'email:~work@example.com:n1@work.example.com', message: {} });
  await p;
  assert.deepEqual(index(), { account: WORK, waiting: [PRIV] }, 'the index still knows who holds a mail');
  ({ el, root } = openSheet(ONLY_WORK, true));
  assert.equal(el._account, WORK);
  assert.equal(el._text, '');
  assert.ok(root.innerHTML.includes(WAITS), 'said again, every time');
  el.close();
  // … and once it can send again, its mail comes back, as the sheet said it
  // would: the sheet opens on it, from the mailbox it was written from.
  ({ el, root } = openSheet(BOTH, true));
  assert.equal(el._account, PRIV);
  assert.equal(el._text, 'A private matter');
  assert.deepEqual(el._chips.to.map((r) => r.address), ['ann@example.com']);
  assert.ok(root.innerHTML.includes('The mail you started earlier is back.'));
  assert.ok(root.innerHTML.includes(`<option value="${PRIV}" selected>`));
  assert.ok(!root.innerHTML.includes('is kept.'), 'nothing waits elsewhere');
  p = el._submit();
  const sent = take((w) => w.method === 'POST', 'the compose');
  assert.equal(sent.body.account, PRIV, 'and leaves from it');
  sent.answer({ id: 'email:~me@example.org:n2@me.example.org', message: {} });
  await p;
  // Sent: nothing waits any more, and nothing is said.
  ({ el, root } = openSheet(BOTH, true));
  assert.ok(!root.innerHTML.includes('is kept.') && !root.innerHTML.includes('is back.'));
  assert.equal(el._text, '');
  el.close();
  // A work mail only STARTED meanwhile: the sheet opens on it, the mailbox
  // written from last — and says where the other mail is and how to get to
  // it, the mailbox being on offer.
  ({ el, root } = parkPrivate());
  write(root, 'bob@builder.example,', 'Work in progress');
  el.close();
  assert.deepEqual(index(), { account: WORK, waiting: [PRIV] });
  ({ el, root } = openSheet(BOTH, true));
  assert.equal(el._account, WORK);
  assert.equal(el._text, 'Work in progress');
  assert.ok(root.innerHTML.includes(THERE), root.innerHTML.slice(0, 1200));
  assert.ok(!root.innerHTML.includes(WAITS));
  // Chosen under From, it comes back; the work mail is the one kept now.
  pick(root, PRIV);
  assert.equal(el._text, 'A private matter');
  assert.deepEqual(index(), { account: PRIV, waiting: [WORK] });
  assert.ok(el.shadowRoot.innerHTML.includes('A mail you started from work@example.com is kept. Choose that mailbox under From to go on with it.'));
  // Discarded: it is no longer waiting anywhere, and the next opening goes
  // to the mail that is.
  el._discard();
  el.close();
  ({ el, root } = openSheet(BOTH, true));
  assert.equal(el._account, WORK);
  assert.equal(el._text, 'Work in progress');
  assert.ok(!root.innerHTML.includes('is kept.'));
  assert.deepEqual(index(), { account: PRIV, waiting: [WORK] }, 'the index is pruned, not rewritten: nothing was written');
  // Opening is not writing: the mailbox written from last stays that one
  // while it is away, and the sheet is back on it once it is offered again.
  ({ el, root } = openSheet(BOTH));
  pick(root, PRIV);
  write(root, 'ann@example.com,', 'From my own address');
  p = el._submit();
  take((w) => w.method === 'POST', 'the compose').answer({ id: 'email:~me@example.org:n3@me.example.org', message: {} });
  await p;
  ({ el, root } = openSheet(ONLY_WORK, true));
  assert.equal(el._account, WORK);
  el.close();
  assert.deepEqual(index(), { account: PRIV, waiting: [] });
  ({ el, root } = openSheet(BOTH, true));
  assert.equal(el._account, PRIV);
  el.close();
  // What the device holds is read as data: names that are none are dropped,
  // one is not listed twice, and a name is text.
  const EVIL = 'a"><img src=x onerror=alert(1)>@x.org';
  const slot = (a) => store.set(`retinue-draft:compose:email:${a.toLowerCase()}`, JSON.stringify({ at: Date.now(),
    v: { to: [{ address: 'ann@example.com' }], cc: [], typed: { to: '', cc: '' }, subject: '', text: 'kept', account: a } }));
  store.clear();
  slot(PRIV);
  slot(EVIL);
  store.set(INDEX, JSON.stringify({ at: Date.now(),
    v: { account: WORK, waiting: [PRIV, PRIV.toUpperCase(), 7, null, '', 'x'.repeat(300), 'empty@example.org', EVIL] } }));
  ({ el, root } = openSheet(ONLY_WORK, true));
  assert.deepEqual(el._parked, [PRIV, EVIL]);
  assert.equal(root.innerHTML.split(WAITS).length - 1, 1, 'named once');
  assert.ok(root.innerHTML.includes('a&quot;&gt;&lt;img src=x onerror=alert(1)&gt;@x.org'));
  assert.ok(!root.innerHTML.includes('<img'));
  store.set(INDEX, JSON.stringify({ at: Date.now(), v: { account: WORK, waiting: 'me@example.org' } }));
  ({ el, root } = openSheet(ONLY_WORK, true));
  assert.deepEqual(el._parked, []);
});

await ok('the sheet: a press made again loses its key only on the mail gateway\'s word', async () => {
  // A first press the dashboard refused for itself: "Not sent", and the key
  // stays with the mail — nothing went out under it, so the next opening
  // does not call it unconfirmed, and the next press is "Not sent" again.
  let { el, root } = openSheet();
  write(root);
  let p = el._submit();
  let req = take((w) => w.method === 'POST', 'the compose');
  const k0 = req.body.request_key;
  req.answer({ error: 'The mail gateway could not be reached; nothing was sent.', sent: false, request_key: k0, by: 'dashboard' }, 502);
  await p;
  assert.equal(q(root, '[data-err="form"]').textContent, 'Not sent: The mail gateway could not be reached; nothing was sent.');
  assert.equal(kept().press.key, k0);
  assert.equal(kept().press.sure, true);
  el.close();
  ({ el, root } = openSheet(ONE, true));
  assert.ok(root.innerHTML.includes('The mail you started earlier is back.</p>'), 'nothing said of a press that sent nothing');
  p = el._submit();
  req = take((w) => w.method === 'POST', 'the compose');
  assert.equal(req.body.request_key, k0);
  req.answer({ error: 'no mail gateway serves that mailbox', sent: false, request_key: k0, by: 'dashboard' }, 409);
  await p;
  assert.equal(q(root, '[data-err="form"]').textContent, 'Not sent: no mail gateway serves that mailbox');
  // A press whose outcome nobody knows, made again and refused by anyone but
  // the mail gateway — the dashboard, a proxy, a server too old to say —
  // was not put to the gateway: the key stays, and it is still not known.
  for (const [doc, status] of [
    [{ error: 'The mail gateway could not be reached; nothing was sent.', sent: false, request_key: 'K', by: 'dashboard' }, 502],
    [{ error: 'no mail gateway serves that mailbox', sent: false, request_key: 'K', by: 'dashboard' }, 409],
    [{ error: 'the gateway has not said which mailbox it serves', sent: false, by: 'dashboard' }, 502],
    [{}, 403],
    [{ error: 'send failed: refused', sent: false, request_key: 'K' }, 424],
  ]) {
    ({ el, root } = openSheet());
    write(root);
    p = el._submit();
    req = take((w) => w.method === 'POST', 'the compose');
    const key = req.body.request_key;
    req.fail();
    await p;
    p = el._submit();
    req = take((w) => w.method === 'POST', 'the compose again');
    assert.equal(req.body.request_key, key);
    req.answer(doc.request_key ? { ...doc, request_key: key } : doc, status);
    await p;
    const why = doc.error || `it answered ${status}`;
    assert.equal(q(root, '[data-err="form"]').textContent, `The server could not be asked about this mail just now (${why}). ` +
      'It is still not known whether the first attempt went out: look for it in the chat list before you send it again.', String(status));
    assert.equal(kept().press.key, key, `${status}: the key stays`);
    assert.equal(kept().press.sure, false);
    el.close();
    ({ el, root } = openSheet(ONE, true));
    assert.ok(root.innerHTML.includes('it is not confirmed whether it went out'), String(status));
    p = el._submit();
    req = take((w) => w.method === 'POST', 'the compose once more');
    assert.equal(req.body.request_key, key, String(status));
    req.answer({ id: 'email:~me@example.org:r@me.example.org', message: {}, repeat: true });
    await p;
  }
  // The mail gateway's own word ends the press, made again or not.
  for (const [doc, status] of [
    [{ error: 'send failed: refused', sent: false, request_key: 'K', by: 'gateway' }, 424],
    [{ error: 'The address does not exist.', by: 'gateway' }, 400],
    [{ error: 'the mail gateway is not configured', sent: false, by: 'gateway' }, 503],
  ]) {
    ({ el, root } = openSheet());
    write(root);
    p = el._submit();
    req = take((w) => w.method === 'POST', 'the compose');
    const key = req.body.request_key;
    req.fail();
    await p;
    p = el._submit();
    req = take((w) => w.method === 'POST', 'the compose again');
    req.answer(doc, status);
    await p;
    assert.equal(q(root, '[data-err="form"]').textContent, `Not sent: ${doc.error}`, String(status));
    assert.equal(kept().press, null, String(status));
    p = el._submit();
    req = take((w) => w.method === 'POST', 'a new press');
    assert.notEqual(req.body.request_key, key, String(status));
    req.answer({ error: 'x', by: 'gateway' }, 400);
    await p;
  }
});

await ok('the sheet: a kept press is asked about again only within the hours its key is known', async () => {
  const HOUR = 3600 * 1000;
  const slot = (press) => store.set(SLOT(), JSON.stringify({ at: Date.now(), v: {
    to: [{ address: 'ann@example.com', name: '' }], cc: [], typed: { to: '', cc: '' }, subject: '', text: 'Hello',
    account: ACCT, press } }));
  const sig = JSON.stringify({ channel: 'email', to: ['ann@example.com'], cc: [], subject: '', text: 'Hello', account: ACCT });
  const K = 'k'.repeat(32);
  // Recent: the same key, and the note that it is not confirmed.
  store.clear();
  slot({ key: K, sig, at: Date.now() - 2 * HOUR });
  let { el, root } = openSheet(ONE, true);
  assert.ok(root.innerHTML.includes('It was being sent when this sheet was last open'));
  let p = el._submit();
  let req = take((w) => w.method === 'POST', 'the compose');
  assert.equal(req.body.request_key, K);
  req.fail();
  await p;
  // From longer ago — or kept without its time, by an older page: the
  // gateway may no longer know the key, so the same request would be a
  // second mail it cannot tell from the first. A new key, and the note says
  // only to look first.
  for (const press of [{ key: K, sig, at: Date.now() - 13 * HOUR }, { key: K, sig }, { key: K, sig, at: 'then' }]) {
    store.clear();
    slot(press);
    ({ el, root } = openSheet(ONE, true));
    assert.equal(el._press, null, JSON.stringify(press));
    assert.ok(!root.innerHTML.includes('It was being sent when this sheet was last open'));
    assert.ok(root.innerHTML.includes('It was being sent a while ago, and it is not known whether it went out: look in the ' +
      'chat list before you send it again — sent now, it is a new mail.'), JSON.stringify(press));
    p = el._submit();
    req = take((w) => w.method === 'POST', 'the compose');
    assert.match(req.body.request_key, KEY_RE);
    assert.notEqual(req.body.request_key, K, JSON.stringify(press));
    req.fail();
    await p;
    assert.ok(!q(root, '[data-err="form"]').textContent.includes('a while ago'));
  }
  // Kept in the open sheet past its hours: the same.
  store.clear();
  ({ el, root } = openSheet());
  write(root);
  p = el._submit();
  req = take((w) => w.method === 'POST', 'the compose');
  const key = req.body.request_key;
  req.fail();
  await p;
  el._press.at = Date.now() - 13 * HOUR;
  p = el._submit();
  req = take((w) => w.method === 'POST', 'the compose again');
  assert.notEqual(req.body.request_key, key);
  assert.ok(Date.now() - kept().press.at < 60 * 1000, 'the new press is timed anew');
  req.fail();
  await p;
});

await ok('a mail kept for a mailbox that is gone is shown, and can be taken into this mail or discarded', async () => {
  const A = 'a@x.org';
  const B = 'b@x.org';
  const C = 'c@x.org';
  const ABC = { ...TWO, accounts: [A, B, C] };
  const AB = { ...TWO, accounts: [A, B] };
  const INDEX = 'retinue-draft:compose:email';
  const index = () => (store.has(INDEX) ? JSON.parse(store.get(INDEX)).v : null);
  const age = (account) => JSON.parse(store.get(SLOT(account))).at;
  const pick = (root, account) => { const a = q(root, '[data-account]'); a.value = account; root.fire('change', a); };
  const park = () => {
    const { el, root } = openSheet(ABC);
    pick(root, C);
    write(root, 'Dora <dora@w.org>, eve@w.org,', 'Important words from c\n<b>and a second line</b>');
    const subj = q(root, '[data-subject]'); subj.value = 'Plans'; root.fire('input', subj);
    el.close();
  };
  // c is removed for good: the sheet names its mail AND shows it — the
  // words are in no other place.
  park();
  let { el, root } = openSheet(AB, true);
  assert.ok(root.innerHTML.includes('A mail you started from c@x.org is kept. It comes back when that mailbox can send again.'));
  let h = root.innerHTML;
  assert.ok(h.includes('<details class="kept" data-kept="c@x.org"><summary>Show the mail from c@x.org</summary>'), h.slice(0, 1500));
  assert.ok(h.includes('<span class="lbl">To</span> Dora &lt;dora@w.org&gt;, eve@w.org'), h);
  assert.ok(h.includes('<span class="lbl">Subject</span> Plans'));
  assert.ok(h.includes('<p class="kept-text">Important words from c\n&lt;b&gt;and a second line&lt;/b&gt;</p>'), 'its words, as text');
  assert.ok(h.includes('data-kept-copy="c@x.org"') && h.includes('data-kept-discard="c@x.org"'));
  // A mailbox that is offered is a choice under From, not shown here.
  el.close();
  const cSlot = store.get(SLOT(C));
  store.set(SLOT(A), cSlot);
  store.set(INDEX, JSON.stringify({ at: Date.now(), v: { account: A, waiting: [C] } }));
  ({ el, root } = openSheet(ABC, true));
  assert.equal(el._account, A);
  assert.ok(root.innerHTML.includes('A mail you started from c@x.org is kept. Choose that mailbox under From to go on with it.'));
  assert.ok(!root.innerHTML.includes('<details class="kept"'), 'nothing to show where the mailbox is on offer');
  el._discard();
  el.close();
  store.set(INDEX, JSON.stringify({ at: Date.now(), v: { account: C, waiting: [] } }));
  // The sheet keeps renewing what it speaks of: a mail written once and
  // spoken of every week since is not dropped at a month.
  store.set(SLOT(C), JSON.stringify({ at: Date.now() - 29 * 24 * 3600 * 1000, v: JSON.parse(store.get(SLOT(C))).v }));
  ({ el, root } = openSheet(AB, true));
  assert.ok(Date.now() - age(C) < 60 * 1000, 'renewed as it is named');
  // Copied into this mail: after what the form holds, never over it — the
  // recipients join, the words follow, the subject where there is none —
  // and the kept one is gone. It leaves from the mailbox under From.
  write(root, 'bob@builder.example,', 'Mine first');
  root.fire('click', { closest: (sel) => (sel === '[data-kept-copy]' ? { getAttribute: () => C } : null) });
  assert.deepEqual(el._chips.to.map((r) => r.address), ['bob@builder.example', 'dora@w.org', 'eve@w.org']);
  assert.equal(el._text, 'Mine first\n\nImportant words from c\n<b>and a second line</b>');
  assert.equal(el._subject, 'Plans');
  assert.equal(store.has(SLOT(C)), false);
  assert.equal(kept(el._account).text, el._text, 'kept under the mailbox it now leaves from');
  assert.ok(!index().waiting.includes(C));
  h = el.shadowRoot.innerHTML;
  assert.ok(h.includes(`The mail you started from c@x.org is in this one now; it leaves from ${el._account}.`), h.slice(0, 900));
  assert.ok(!h.includes('A mail you started from c@x.org is kept.'));
  assert.equal(globalThis.focused, q(el.shadowRoot, '[data-text]'));
  let p = el._submit();
  const req = take((w) => w.method === 'POST', 'the compose');
  assert.equal(req.body.account, el._account);
  assert.deepEqual(req.body.to, ['bob@builder.example', 'dora@w.org', 'eve@w.org']);
  req.answer({ id: 'email:~a@x.org:n@x.org', message: {} });
  await p;
  // A subject of its own leads the copied words where the form has another.
  park();
  ({ el, root } = openSheet(AB, true));
  const subj = q(root, '[data-subject]'); subj.value = 'Mine'; root.fire('input', subj);
  el._copyKept(C);
  assert.equal(el._subject, 'Mine');
  assert.equal(el._text, 'Plans\n\nImportant words from c\n<b>and a second line</b>');
  el._discard();
  el.close();
  // Discarded: gone, and said; the mailbox written from last stays that one.
  park();
  ({ el, root } = openSheet(AB, true));
  const before = index().account;
  root.fire('click', { closest: (sel) => (sel === '[data-kept-discard]' ? { getAttribute: () => C } : null) });
  assert.equal(store.has(SLOT(C)), false);
  assert.deepEqual(index(), { account: before, waiting: [] });
  assert.ok(el.shadowRoot.innerHTML.includes('The mail you started from c@x.org is discarded.'));
  assert.equal(el._text, '');
  el.close();
  ({ el, root } = openSheet(AB, true));
  assert.ok(!root.innerHTML.includes('c@x.org'));
  // Nothing is done for a mailbox that is not waiting, nor while sending.
  park();
  ({ el, root } = openSheet(AB, true));
  el._copyKept(A);
  el._discardKept('nobody@x.org');
  assert.ok(store.has(SLOT(C)));
  write(root);
  p = el._submit();
  el._copyKept(C);
  el._discardKept(C);
  assert.ok(store.has(SLOT(C)), 'untouched while a mail is on its way');
  take((w) => w.method === 'POST', 'the compose').answer({ error: 'x', by: 'gateway' }, 400);
  await p;
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
    # Whom a reply reaches is never worked out by the page.
    if "replyRecipients" in src:
        errors.append("chat-page.js still derives a reply's recipients (replyRecipients)")
    if "/reply-preview" not in code:
        errors.append("chat-page.js no longer asks the server where a reply goes")
    # The controls of a failed or unconfirmed send are wired where every
    # bubble's controls are: the thread's delegated click handler.
    for control in ("[data-pend-copy]", "[data-pend-dismiss]", "[data-recheck]"):
        if f"at('{control}')" not in code:
            errors.append(f"the thread's click handler does not know {control}")
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
