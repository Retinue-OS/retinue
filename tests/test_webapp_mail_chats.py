#!/usr/bin/env python3
"""Mail threads as chats in the dashboard (webapp/components/chats.js and
chat-page.js; docs/mail-chats.md, "What a chat shows" and "Trimmed bubbles,
the original one tap away").

Both elements are driven under Node with a scripted DOM and a scripted fetch,
and pin:

  * the row: the subject as the name, the participants line ("+n" past the
    server's cap), the @ channel mark, the avatar keyed on the peer's address
    (one colour per person across threads) with the peer's initials, the
    preview "Name: text" and a removed head shown as removed, "You (mail
    client)" for a mail sent from another client;
  * the account rule: a name repeated across accounts carries the account, a
    name repeated on ONE account (two threads with one subject) does not —
    messenger chats as before;
  * the header: subject, participants and account, never the Message-ID key;
  * bubbles: the sender's name in a group (the address where the mail gave
    none), none in a 1:1, a mail's own subject where it is not the thread's
    (reply markers told by shape, in any language), "You · mail client", the
    trimmed text linkified under the http(s)-only rule with its line breaks;
  * Original: offered only for the chat's own original path; a tap fetches it
    once and expands it in place (headers, the full header block behind a
    disclosure, the HTML part in a sandboxed frame built by DOM calls for the
    exact own `.html` path only, else the full plain text, every attachment);
    a second tap collapses it, a third shows it from the cache; it survives a
    poll and a full render; loading and error states, with a retry;
  * a tombstone: quiet, who and when, no Original and no attachments;
  * the poll reconciles: a message on screen whose removal, text, trim or
    attachments changed is re-rendered in place, the other bubbles and the
    scroll position untouched, an open Original kept where it still applies;
  * `can_send: false` replaces the composer, the chips and the draft with the
    server's reason, drops the grip, adopts and saves no draft; a flip
    re-renders;
  * messenger chats render as before;
  * the reference documents (webapp/data/chats*) carry a mail thread that
    renders: a group thread with an inline image, an HTML mail, a tombstone.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes (the static checks still run).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(HERE, "..", "webapp", "components")
CHAT_PAGE = os.path.join(COMPONENTS, "chat-page.js")
DATA = os.path.join(HERE, "..", "webapp", "data")
FIXTURE = os.path.join(DATA, "chats", "email-quarterly.json")
ORIGINAL_FIXTURE = os.path.join(DATA, "chats", "email-quarterly.original.json")

HARNESS = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// ── A scripted DOM ───────────────────────────────────────────────────────────
// The thread is an ordered list of top-level nodes, each the HTML of one
// <div> the page put there. A node answers the few queries the page makes of
// a bubble: its frame slot (which records what is appended to it) and its
// Original control (which records focus).
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
  constructor(attr) { this.attr = attr; }
  focus() { globalThis.focused = this; document.activeElement = this; }
  hasAttribute(n) { return n === this.attr; }
}
class Slot {
  constructor() { this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
}
class Frame {
  constructor() { this.attrs = new Map(); this.className = ''; this.style = {}; }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  getAttribute(n) { return this.attrs.has(n) ? this.attrs.get(n) : null; }
}
const frames = [];
class Node {
  constructor(thread, html) {
    this.thread = thread;
    this.html = html;
    const open = html.slice(0, html.indexOf('>') + 1);
    this.mid = (/data-mid="([^"]*)"/.exec(open) || [])[1] || null;
    if (this.mid) this.mid = this.mid.replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>');
    this.isRow = open.includes('data-mark-read-row');
    this.button = this.isRow ? new Ctl('data-mark-read') : null;
    this._slot = null;
    this._toggle = null;
  }
  getAttribute(n) { return n === 'data-mid' ? this.mid : null; }
  hasAttribute() { return false; }
  setAttribute() {}
  focus() {}
  querySelector(sel) {
    if (sel === '[data-orig-frame]') {
      return this.html.includes('data-orig-frame') ? (this._slot = this._slot || new Slot()) : null;
    }
    if (sel === '[data-orig-toggle]') {
      return this.html.includes('data-orig-toggle') ? (this._toggle = this._toggle || new Ctl('data-orig-toggle')) : null;
    }
    return null;
  }
  contains(x) { return !!x && (x === this._toggle || x === this.button); }
  remove() { this.thread.nodes = this.thread.nodes.filter((x) => x !== this); }
  replaceWith(n) { const i = this.thread.nodes.indexOf(this); this.thread.nodes[i] = n; n.thread = this.thread; }
  insertAdjacentHTML(where, html) {
    assert.equal(where, 'afterend');
    const i = this.thread.nodes.indexOf(this);
    this.thread.nodes.splice(i + 1, 0, ...chunks(html).map((h) => new Node(this.thread, h)));
  }
}
class Thread {
  constructor(html) {
    this.nodes = chunks(html).map((h) => new Node(this, h));
    this.scrollTop = 0;
    this.clientHeight = 500;
    this.attrs = new Map();
  }
  hasAttribute(n) { return this.attrs.has(n); }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  focus() {}
  get scrollHeight() { return 1000; }
  querySelector(sel) {
    if (sel === '[data-mark-read-row]') return this.nodes.find((n) => n.isRow) || null;
    if (sel === '[data-mark-read-row] button') { const r = this.nodes.find((n) => n.isRow); return r ? r.button : null; }
    return null;
  }
  querySelectorAll(sel) { return sel === '[data-mid]' ? this.nodes.filter((n) => n.mid) : []; }
  insertAdjacentHTML(where, html) {
    assert.equal(where, 'beforeend');
    this.nodes.push(...chunks(html).map((h) => new Node(this, h)));
  }
  node(mid) { return this.nodes.find((n) => n.mid === mid); }
  layout() { return this.nodes.map((n) => (n.mid ? n.mid : n.isRow ? 'ROW' : null)).filter(Boolean); }
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
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
globalThis.document = {
  addEventListener() {}, removeEventListener() {},
  createElement: (tag) => {
    if (tag === 'iframe') { const f = new Frame(); frames.push(f); return f; }
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
  querySelector() { return null; }, getElementById() { return null; },
  hidden: false, visibilityState: 'visible', body: { appendChild() {} },
  documentElement: { dataset: {} }, title: 'Retinue — Chat', activeElement: null,
};
globalThis.window = globalThis;
globalThis.innerHeight = 800;
globalThis.location = { hash: '', pathname: '/chat.html', search: '', href: 'http://dash/chat.html' };
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.dispatchEvent = () => true;
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms, ...rest) => (ms >= 1000 ? 0 : realSetTimeout(fn, ms, ...rest));
globalThis.history = { length: 1, pushState() {}, replaceState() {}, back() {} };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
const store = new Map();
globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
globalThis.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', mediaDevices: null }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };

// ── A scripted gateway ───────────────────────────────────────────────────────
const wire = [];
const log = [];
globalThis.fetch = (url, init = {}) => {
  const req = { url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null };
  log.push(req);
  return new Promise((resolve) => {
    req.answer = (doc, status = 200) => resolve({ ok: status < 400, status, json: async () => doc });
    wire.push(req);
  });
};
const settle = () => new Promise((r) => realSetTimeout(r, 0));
const take = (pred, what) => {
  const i = wire.findIndex(pred);
  assert.ok(i >= 0, `expected ${what}, saw ${JSON.stringify(wire.map((w) => [w.method, w.url]))}`);
  return wire.splice(i, 1)[0];
};

await import('./chat-page.js');
const Page = registry.get('retinue-chat-page');
const List = registry.get('retinue-chats');
const chats = await import('./chats.js');
const { CHANNELS, colorFor, initials, previewHtml, participantsText, avatarHtml } = chats;
const { shelf } = await import('./shelf-store.js');
shelf.observe = () => {};

let passed = 0;
async function ok(name, fn) { await fn(); passed += 1; console.log('  ok  ' + name); }

// ── The list (chats.js) ──────────────────────────────────────────────────────
const ACCT = 'me@example.org';
const mailChat = (key, extra = {}) => ({
  id: `email:~${extra.account || ACCT}:${key}`, channel: 'email', account: ACCT, key,
  name: 'Quarterly numbers', group: true,
  participants: [{ address: 'ann@example.com', name: 'Ann Example', contact: null },
                 { address: 'bob@builder.example', name: 'Bob Builder', contact: null }],
  participants_more: 0,
  peer: { address: 'ann@example.com', name: 'Ann Example' },
  unread: 1, list_group: 'unread', inbound_seq: 3, archived: false, muted: false, starred: false,
  last: { ts: '2026-11-20T10:00:00Z', direction: 'in', kind: 'text', text: 'See the table', sender_name: 'Ann Example', removed: false },
  draft: null, companion: null, messages: `/chats/x/messages`, ...extra,
});
const sigChat = (id, extra = {}) => ({
  id, channel: 'signal', account: '+41000', key: '+41999', name: 'Mara', group: false,
  unread: 1, list_group: 'unread', inbound_seq: 2, archived: false, muted: false, starred: false,
  last: { ts: '2026-11-20T09:00:00Z', direction: 'in', kind: 'text', text: 'hi', sender_name: 'Mara' },
  draft: null, companion: null, messages: `/chats/y/messages`, ...extra,
});
function listHtml(list, scope = 'unread') {
  const el = new List();
  el._full = true;
  el._scope = scope;
  el._dirty = false;
  return el.body({ chats: JSON.parse(JSON.stringify(list)) });
}
function rowOf(html, id) {
  const start = html.indexOf(`<li class="swipe" data-id="${id}"`);
  assert.ok(start >= 0, `row ${id} rendered`);
  return html.slice(html.indexOf('<a class="row', start), html.indexOf('</li>', start));
}

await ok('e-mail is a channel with an @ mark and a colour of its own', async () => {
  assert.equal(CHANNELS.email.label, 'E-mail');
  assert.equal(CHANNELS.email.mark, '@');
  const others = Object.entries(CHANNELS).filter(([k]) => k !== 'email').map(([, c]) => c.color);
  assert.ok(!others.includes(CHANNELS.email.color));
  assert.ok(chats.channelMarkHtml('email').includes('>@</span>'));
});

await ok('a mail row: subject, participants, peer avatar, "Name: text"', async () => {
  const c = mailChat('root1@x');
  const row = rowOf(listHtml([c]), c.id);
  assert.ok(row.includes('class="row has-unread has-people"'), row);
  assert.ok(row.includes('<span class="name">Quarterly numbers</span>'), row);
  assert.ok(row.includes('<span class="ppl" title="Ann Example, Bob Builder">Ann Example, Bob Builder</span>'), row);
  assert.ok(row.includes('<span class="pv-who">Ann:</span> See the table'), 'a mail names its sender even in a 1:1 sense');
  assert.ok(row.includes(`background:${colorFor('email\u0000ann@example.com')}`), 'coloured by the peer');
  assert.ok(row.includes('>AE<span class="ch"'), 'the peer\'s initials');
  assert.ok(!row.includes('root1@x'), 'the Message-ID key is never shown');
  // Not a group: the preview still says who wrote.
  assert.ok(previewHtml(mailChat('r', { group: false })).startsWith('<span class="pv-who">Ann:</span>'));
});

await ok('participants past the cap read "+n"; a name-less participant is their address', async () => {
  const six = ['a', 'b', 'c', 'd', 'e', 'f'].map((x) => ({ address: `${x}@x.org`, name: x === 'c' ? null : x.toUpperCase() }));
  assert.equal(participantsText({ participants: six, participants_more: 3 }), 'A, B, c@x.org, D, E, F +3');
  assert.equal(participantsText({ participants: [], participants_more: 2 }), '+2');
  assert.equal(participantsText({}), '', 'a messenger chat names none');
  assert.equal(participantsText({ participants: [null, 'x', { name: ' ' }] }), '');
  const row = rowOf(listHtml([mailChat('k', { participants: six, participants_more: 3 })]), 'email:~me@example.org:k');
  assert.ok(row.includes('>A, B, c@x.org, D, E, F +3</span>'), row);
});

await ok('the avatar follows the person across threads, not the thread', async () => {
  const one = avatarHtml(mailChat('root1@x'));
  const two = avatarHtml(mailChat('root2@y', { name: 'Something else', peer: { address: 'Ann@Example.com', name: 'Ann E.' } }));
  const colour = (h) => /background:([^"]+)"/.exec(h)[1];
  assert.equal(colour(one), colour(two), 'one colour for one address, whatever its case');
  const bob = avatarHtml(mailChat('root1@x', { peer: { address: 'bob@builder.example', name: 'Bob Builder' } }));
  assert.equal(colour(bob), colorFor('email\u0000bob@builder.example'));
  // A peer without a name: initials of the address.
  assert.ok(avatarHtml(mailChat('r', { peer: { address: 'zoe@x.org', name: null } })).includes('>Z<span'));
  // Messenger avatars as before: keyed on the chat key, initials of the name.
  const sig = avatarHtml(sigChat('s1'));
  assert.equal(colour(sig), colorFor('signal\u0000+41999'));
  assert.ok(sig.includes(`>${initials('Mara')}<span`));
});

await ok('the preview: a removed head is shown as removed; a mail from another client', async () => {
  const gone = mailChat('k', { last: { ts: 't', direction: 'in', kind: 'text', text: '', sender_name: 'Ann Example', removed: true } });
  assert.equal(previewHtml(gone), '<span class="pv-who">Ann:</span> <span class="pv-removed">Removed in a mail client</span>');
  const sent = mailChat('k', { last: { ts: 't', direction: 'out', author: 'device', kind: 'text', text: 'ok' } });
  assert.equal(previewHtml(sent), '<span class="pv-who">You (mail client):</span> ok');
  const sigSent = sigChat('s', { last: { ts: 't', direction: 'out', author: 'device', kind: 'text', text: 'ok' } });
  assert.equal(previewHtml(sigSent), '<span class="pv-who">You (phone):</span> ok');
  const hostile = mailChat('k', { last: { ts: 't', direction: 'in', text: '<img src=x>', sender_name: '<b>Eve</b>' } });
  assert.equal(previewHtml(hostile), '<span class="pv-who">&lt;b&gt;Eve&lt;/b&gt;:</span> &lt;img src=x&gt;');
});

await ok('the account shows where a name repeats across accounts — not on one account', async () => {
  // One thread that reached two of the user's mailboxes: two chats, one name.
  const a = mailChat('same@x', { id: 'email:~me@example.org:same@x' });
  const b = mailChat('same@x', { id: 'email:~work@example.com:same@x', account: 'work@example.com' });
  let h = listHtml([a, b]);
  assert.ok(rowOf(h, a.id).includes('<span class="acct" title="On the account me@example.org">me@example.org</span>'));
  assert.ok(rowOf(h, b.id).includes('>work@example.com</span>'));
  // Two different threads on ONE account that share a subject: no account.
  const c = mailChat('t1@x');
  const d = mailChat('t2@x');
  h = listHtml([c, d]);
  assert.ok(!rowOf(h, c.id).includes('class="acct"'), rowOf(h, c.id));
  assert.ok(!rowOf(h, d.id).includes('class="acct"'));
  // Messenger as before: one peer on two accounts, and the unattributed
  // history beside an account-named chat.
  h = listHtml([sigChat('signal:~+41000:+41999'), sigChat('signal:~+41001:+41999', { account: '+41001' }),
                sigChat('signal:+41999', { account: null })]);
  assert.ok(rowOf(h, 'signal:~+41000:+41999').includes('>+41000</span>'));
  assert.ok(rowOf(h, 'signal:~+41001:+41999').includes('>+41001</span>'));
  assert.ok(rowOf(h, 'signal:+41999').includes('>earlier</span>'));
  // And a name repeated across channels is not a repeat.
  h = listHtml([sigChat('signal:~+41000:+41999', { name: 'Quarterly numbers' }), mailChat('q@x')]);
  assert.ok(!h.includes('class="acct"'));
});

await ok('a messenger row is unchanged: no participants line, first names in a group only', async () => {
  const row = rowOf(listHtml([sigChat('s1')]), 's1');
  assert.ok(row.startsWith('<a class="row has-unread" draggable="false" href="/chat.html?id=s1">'), row);
  assert.ok(!row.includes('class="ppl"'));
  assert.ok(row.includes('<span class="prev">hi</span>'), 'a 1:1 names nobody');
  const grp = sigChat('g', { group: true, last: { ts: 't', direction: 'in', text: 'yo', sender_name: 'Beat Odermatt' } });
  assert.equal(previewHtml(grp), '<span class="pv-who">Beat:</span> yo');
});

// ── The chat page ────────────────────────────────────────────────────────────
const ID = 'email:~me@example.org:root@mail.example.com';
const ENC = encodeURIComponent(ID);
const MSG_URL = `/chats/${ENC}/messages`;
const orig = (tok) => `/chats/${ENC}/messages/${tok}/original`;
const pageSummary = (extra = {}) => ({
  id: ID, channel: 'email', account: ACCT, key: 'root@mail.example.com', name: 'Quarterly numbers', group: true,
  participants: [{ address: 'ann@example.com', name: 'Ann Example', contact: null },
                 { address: 'bob@builder.example', name: 'Bob Builder', contact: null }],
  participants_more: 0, peer: { address: 'ann@example.com', name: 'Ann Example' },
  unread: 0, list_group: 'read', last_read: null, inbound_seq: 3, archived: false, muted: false, starred: false,
  companion: null, instructed: null, draft: null, messages: MSG_URL,
  can_send: false, cannot_send_reason: 'Mail is read-only <for now>.', ...extra,
});
const mail = (id, direction, ts, extra = {}) => ({
  id, chat: ID, direction, ts, text: `text of ${id}`, sender: 'ann@example.com', sender_name: 'Ann Example',
  subject: 'Re: Quarterly numbers', recipients: [ACCT], cc: [], trim: 'deterministic',
  removed: false, removed_at: null, original: orig(`t${id}`), attachments: [], ...extra,
});
const BASE = [
  mail('m1', 'in', '2026-11-20T08:00:00Z', { subject: 'Quarterly numbers' }),
  mail('m2', 'out', '2026-11-20T08:05:00Z', { author: 'device', sender: ACCT, sender_name: 'Me' }),
  mail('m3', 'in', '2026-11-20T09:00:00Z', { sender: 'bob@builder.example', sender_name: 'Bob Builder',
                                              subject: 'Re: Quarterly numbers (revised)' }),
  mail('m4', 'in', '2026-11-20T09:30:00Z', { sender: 'carla@example.net', sender_name: null }),
];
let renders = 0;
async function open(chat = {}, messages = BASE, id = ID) {
  wire.length = 0;
  log.length = 0;
  frames.length = 0;
  renders = 0;
  const el = new Page();
  el.attachShadow({ mode: 'open' });
  el._id = id;
  // The page's full rebuild, as far as the thread goes: the thread from
  // _chatThreadHtml, then the frames of what is left open — the two steps of
  // render() that concern the mirror (render's own source is checked to make
  // the second call, below).
  el.render = function () {
    renders += 1;
    const thread = this._state === 'ok' ? new Thread(this._chatThreadHtml()) : null;
    this.shadowRoot = { querySelector: (s) => (s === '[data-chat-thread]' ? thread : null), querySelectorAll: () => [] };
    this._thread = thread;
    if (thread) this._mountFrames();
  };
  const loading = el._load();
  take((w) => w.url === '/chats', 'the list').answer({ chats: [pageSummary(chat)] });
  await settle();
  take((w) => w.url === MSG_URL || w.url === (chat.messages || ''), 'the messages')
    .answer({ chat: pageSummary(chat), messages: messages.map((m) => JSON.parse(JSON.stringify(m))) });
  await loading;
  clearInterval(el._pollTimer);
  return el;
}
async function poll(el, chat, messages) {
  const p = el._poll();
  take((w) => w.url === el._msgUrl, 'the poll')
    .answer({ chat: pageSummary(chat), messages: messages.map((m) => JSON.parse(JSON.stringify(m))) });
  await p;
}
const bubble = (el, mid) => { const n = el._thread.node(mid); assert.ok(n, `bubble ${mid}`); return n.html; };
const DOC = (tok, extra = {}) => ({
  id: 'x',
  headers: { from: 'Bob Builder <bob@builder.example>', to: 'me@example.org', cc: 'carla@example.net',
             date: 'Fri, 20 Nov 2026 10:00:00 +0100', subject: 'Re: Quarterly numbers (revised)' },
  header_block: 'From: Bob Builder <bob@builder.example>\nX-Mailer: <script>x</script>',
  text: 'Full text\nwith the quote\n> earlier https://example.org/a',
  html_url: `${orig(tok)}.html`,
  attachments: [{ id: 'a', type: 'application/pdf', name: 'table.pdf', size: 2048,
                  url: '/chats/media/mail-gateway/' + 'ab'.repeat(16) }],
  ...extra,
});
async function expand(el, mid, doc) {
  el._toggleOriginal(mid);
  const req = take((w) => w.method === 'GET' && w.url.endsWith('/original'), 'the original');
  if (doc !== undefined) {
    req.answer(doc);
    await settle();
    await settle();
  }
  return req;
}

await ok('the header: subject, participants and account — never the Message-ID', async () => {
  const el = await open();
  const h = el._headHtml();
  assert.ok(h.includes('<div class="head-name">Quarterly numbers</div>'), h);
  assert.ok(h.includes('<small class="head-sub" title="Ann Example, Bob Builder">Ann Example, Bob Builder</small>'), h);
  assert.ok(h.includes('<small class="head-sub head-acct">via me@example.org</small>'), h);
  assert.ok(!h.includes('root@mail.example.com'), 'no key');
  assert.ok(h.includes(`background:${colorFor('email\u0000ann@example.com')}`), 'the peer\'s colour');
  // Without participants: the channel, still no key.
  el._chat.participants = [];
  assert.ok(el._headHtml().includes('<small class="head-sub">E-mail</small>'));
  assert.ok(!el._headHtml().includes('root@mail.example.com'));
  assert.deepEqual(el._avatar(), { text: 'AE', color: colorFor('email\u0000ann@example.com') });
});

await ok('bubbles: sender names in a group, a differing subject, "You · mail client", the text', async () => {
  const el = await open({}, [
    ...BASE,
    mail('m5', 'in', '2026-11-20T10:00:00Z', {
      text: 'see https://example.org/x.\nline two <b>bold</b> javascript:alert(1)', subject: 'AW: Re: quarterly  numbers' }),
    mail('m6', 'in', '2026-11-20T10:01:00Z', { subject: '回复：Quarterly numbers' }),
    mail('m7', 'in', '2026-11-20T10:02:00Z', { subject: '[fin] Re[2]: Quarterly numbers' }),
    mail('m8', 'out', '2026-11-20T10:03:00Z', { author: 'agent', agent: 'Ara' }),
  ]);
  assert.ok(bubble(el, 'm1').includes('>Ann Example</small>'), 'sender name');
  assert.ok(!bubble(el, 'm1').includes('msg-subj'), 'the thread\'s own subject: no line');
  assert.ok(bubble(el, 'm3').includes('<div class="msg-subj">Re: Quarterly numbers (revised)</div>'), bubble(el, 'm3'));
  assert.ok(bubble(el, 'm4').includes('>carla@example.net</small>'), 'no display name: the address');
  assert.ok(bubble(el, 'm2').includes('<small class="who">You &middot; mail client</small>'), bubble(el, 'm2'));
  assert.ok(bubble(el, 'm8').includes('<small class="who">Ara</small>'));
  for (const id of ['m5', 'm6', 'm7']) assert.ok(!bubble(el, id).includes('msg-subj'), `${id}: a reply marker is not a new subject`);
  const t = bubble(el, 'm5');
  assert.ok(t.includes('<a href="https://example.org/x" target="_blank" rel="noopener noreferrer">https://example.org/x</a>.<br>line two &lt;b&gt;bold&lt;/b&gt; javascript:alert(1)'), t);
  // A 1:1 thread names nobody on an inbound bubble, as every channel.
  const solo = await open({ group: false });
  assert.ok(!bubble(solo, 'm1').includes('class="who sender"'));
});

await ok('Original is offered only for the chat\'s own original path', async () => {
  const bad = [
    `/chats/${encodeURIComponent('email:~me@example.org:other@x')}/messages/abc/original`,
    'https://evil.example/chats/x/messages/abc/original',
    `//evil.example/chats/${ENC}/messages/abc/original`,
    `/chats/${ENC}/messages/%2e%2e/original`,
    `/chats/${ENC}/messages/..%2F/original`,
    `/chats/${ENC}/messages/a/b/original`,
    `/chats/${ENC}/messages/abc/original?x=1`,
    `/chats/${ENC}/messages/abc/original.html`,
    `/chats/${ENC}/messages//original`,
    `/chats/${ENC}%zz/messages/abc/original`,
    null, 42,
  ];
  const msgs = bad.map((u, i) => mail(`b${i}`, 'in', `2026-11-20T08:0${i % 10}:00Z`, { original: u }));
  const el = await open({}, [...msgs, mail('good', 'in', '2026-11-20T09:00:00Z', { original: orig('Zz9._~-%41') })]);
  for (let i = 0; i < bad.length; i += 1) {
    assert.ok(!bubble(el, `b${i}`).includes('data-orig-toggle'), `no Original for ${bad[i]}`);
    el._toggleOriginal(`b${i}`);
  }
  assert.equal(wire.length, 0, 'nothing fetched for a foreign path');
  // The server's percent-encoding may differ from the browser's: decoded,
  // the id is what counts.
  const g = bubble(el, 'good');
  assert.ok(g.includes('<button type="button" class="orig-btn" data-orig-toggle aria-expanded="false"'), g);
  assert.ok(g.includes('>Original</button>'));
  const py = `/chats/${ID.replace(/:/g, '%3A').replace(/@/g, '%40')}/messages/k/original`;
  const el2 = await open({}, [mail('p', 'in', '2026-11-20T09:00:00Z', { original: py })]);
  assert.ok(bubble(el2, 'p').includes('data-orig-toggle'));
});

await ok('a tap expands the whole mail in place; a second collapses it; a third is cached', async () => {
  const el = await open();
  const req = await expand(el, 'm3');
  assert.equal(req.url, orig('tm3'), 'the URL the message names, verbatim');
  assert.ok(bubble(el, 'm3').includes('Loading the original'), 'a loading state');
  req.answer(DOC('tm3'));
  await settle();
  await settle();
  const b = bubble(el, 'm3');
  assert.ok(b.startsWith('<div class="msg in orig-open" data-mid="m3">'), b);
  assert.ok(b.includes('<dt>From</dt><dd>Bob Builder &lt;bob@builder.example&gt;</dd>'), b);
  for (const k of ['To', 'Cc', 'Date', 'Subject']) assert.ok(b.includes(`<dt>${k}</dt>`), k);
  assert.ok(b.includes('<details class="orig-raw" data-orig-raw><summary>All headers</summary>' +
    '<pre>From: Bob Builder &lt;bob@builder.example&gt;\nX-Mailer: &lt;script&gt;x&lt;/script&gt;</pre></details>'), b);
  assert.ok(/<div class="orig-frame" data-orig-frame style="height:\d+px"><\/div>/.test(b), 'the frame\'s slot');
  assert.ok(!b.includes('<iframe'), 'the frame is no markup');
  assert.ok(b.includes('<a class="att-file" href="/chats/media/mail-gateway/' + 'ab'.repeat(16) + '"'), 'its attachments');
  assert.ok(!b.includes('text of m3'), 'the trimmed text gives way');
  assert.ok(b.includes('aria-expanded="true"') && b.includes('>Hide original</button>'));
  // The frame: built by DOM calls, sandboxed, the original's own .html.
  const slot = el._thread.node('m3')._slot;
  assert.equal(slot.children.length, 1);
  const f = slot.children[0];
  assert.equal(f.getAttribute('sandbox'), 'allow-popups allow-popups-to-escape-sandbox');
  assert.equal(f.getAttribute('src'), `${orig('tm3')}.html`);
  assert.equal(f.getAttribute('title'), 'The original mail: Re: Quarterly numbers (revised)');
  assert.equal(f.getAttribute('referrerpolicy'), 'no-referrer');
  assert.equal(f.className, 'orig-html');
  // Collapse: no fetch, the trimmed text is back.
  el._toggleOriginal('m3');
  assert.equal(wire.length, 0);
  assert.ok(bubble(el, 'm3').includes('text of m3'));
  assert.ok(bubble(el, 'm3').includes('aria-expanded="false"'));
  assert.ok(!bubble(el, 'm3').includes('class="orig"'));
  // Again: from the cache.
  el._toggleOriginal('m3');
  assert.equal(wire.length, 0, 'fetched once per message');
  assert.equal(el._thread.node('m3')._slot.children[0].getAttribute('src'), `${orig('tm3')}.html`);
});

await ok('focus stays on the Original control; the scroll offset is kept', async () => {
  const el = await open();
  el._thread.scrollTop = 120;
  document.activeElement = el._thread.node('m1').querySelector('[data-orig-toggle]');
  globalThis.focused = null;
  await expand(el, 'm1', DOC('tm1'));
  assert.equal(el._thread.scrollTop, 120);
  assert.ok(globalThis.focused && globalThis.focused === el._thread.node('m1')._toggle, 'focus on the new control');
  document.activeElement = null;
});

await ok('the open Original survives a poll, a reconciled change and a full render', async () => {
  const el = await open();
  await expand(el, 'm3', DOC('tm3'));
  const before = el._thread.node('m3');
  await poll(el, {}, BASE);
  assert.equal(el._thread.node('m3'), before, 'an unchanged poll touches nothing');
  // A better trim arrives: the bubble re-renders, still open, frame again.
  const better = BASE.map((m) => (m.id === 'm3' ? { ...m, text: 'better', trim: 'model' } : m));
  await poll(el, {}, better);
  assert.notEqual(el._thread.node('m3'), before);
  assert.ok(bubble(el, 'm3').includes('orig-open'));
  assert.equal(el._thread.node('m3')._slot.children.length, 1);
  assert.equal(wire.length, 0, 'no refetch: the original does not change');
  // A full render (a breakpoint flip, a recording starting).
  el.render();
  assert.ok(bubble(el, 'm3').includes('orig-open'));
  assert.equal(el._thread.node('m3')._slot.children[0].getAttribute('src'), `${orig('tm3')}.html`);
  // Collapsed, the better trim shows.
  el._toggleOriginal('m3');
  assert.ok(bubble(el, 'm3').includes('better'));
  // The disclosure's state is kept on the instance too.
  el._origRawOpen.add('m1');
  await expand(el, 'm1', DOC('tm1'));
  assert.ok(bubble(el, 'm1').includes('<details class="orig-raw" data-orig-raw open>'));
});

await ok('the frame only for the exact own .html path; else the full plain text', async () => {
  const cases = [
    ['https://evil.example/x.html', 'foreign'],
    [`${orig('tm1')}.htm`, 'not .html'],
    [`${orig('tm3')}.html`, 'another message\'s'],
    [`${orig('tm1')}.html?x`, 'a query'],
    [`javascript${':'}alert(1)`, 'a script URL'],
    [null, 'none'],
  ];
  for (const [url, what] of cases) {
    const el = await open();
    await expand(el, 'm1', DOC('tm1', { html_url: url }));
    const b = bubble(el, 'm1');
    assert.ok(!b.includes('data-orig-frame'), `${what}: no frame`);
    assert.equal(frames.length, 0, `${what}: no frame element`);
    assert.ok(b.includes('<div class="orig-text">Full text<br>with the quote<br>&gt; earlier ' +
      '<a href="https://example.org/a" target="_blank" rel="noopener noreferrer">https://example.org/a</a></div>'), `${what}: ${b}`);
  }
  // Neither HTML nor text.
  const el = await open();
  await expand(el, 'm1', DOC('tm1', { html_url: null, text: '' }));
  assert.ok(bubble(el, 'm1').includes('This mail has no text.'));
});

await ok('a failed original says so and retries; hostile header values are text', async () => {
  const el = await open();
  const req = await expand(el, 'm1');
  req.answer({ error: 'x' }, 502);
  await settle();
  await settle();
  assert.ok(bubble(el, 'm1').includes('Couldn&rsquo;t load the original.'));
  assert.ok(bubble(el, 'm1').includes('data-orig-retry'));
  el._loadOriginal(el._messages.find((m) => m.id === 'm1'), orig('tm1'));
  take((w) => w.url === orig('tm1'), 'the retry').answer(DOC('tm1', {
    headers: { from: '"><img src=x onerror=alert(1)>', subject: 'x' }, html_url: null }));
  await settle();
  await settle();
  const b = bubble(el, 'm1');
  assert.ok(b.includes('<dd>&quot;&gt;&lt;img src=x onerror=alert(1)&gt;</dd>'), b);
  assert.ok(!b.includes('<dt>To</dt>'), 'an absent header is left out');
  // A collapsed, failed original is fetched again on the next tap.
  el._toggleOriginal('m1');
  el._origCache.get('m1').state = 'error';
  el._toggleOriginal('m1');
  assert.ok(wire.some((w) => w.url === orig('tm1')), 'refetched after an error');
});

await ok('a tombstone: quiet, who and when, no Original, no attachments', async () => {
  const gone = mail('g1', 'in', '2026-11-20T09:45:00Z', {
    removed: true, removed_at: '2026-11-21T07:00:00Z', text: '', original: orig('tg1'), sender_name: 'Bob Builder',
    attachments: [{ id: 'a', type: 'image/png', url: '/chats/media/mail-gateway/' + 'cd'.repeat(16) }] });
  const el = await open({}, [...BASE, gone]);
  const b = bubble(el, 'g1');
  assert.ok(b.startsWith('<div class="msg in tomb" data-mid="g1"><div class="bubble" role="note" title="Removed '), b);
  assert.ok(b.includes('<span class="tomb-txt">This mail was removed in a mail client</span>'));
  assert.ok(/<span class="tomb-meta">Bob Builder · [^<]+<\/span>/.test(b), b);
  assert.ok(!b.includes('data-orig-toggle') && !b.includes('att-') && !b.includes('<img'), b);
  el._toggleOriginal('g1');
  assert.equal(wire.length, 0);
});

await ok('the poll re-renders a changed message in place and nothing else', async () => {
  const el = await open({ unread: 1, list_group: 'unread' });
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'm4', 'ROW']);
  await expand(el, 'm3', DOC('tm3'));
  el._thread.scrollTop = 200;
  const kept = Object.fromEntries(['m1', 'm2', 'm4'].map((id) => [id, el._thread.node(id)]));
  // m3 is removed in a mail client; m1's attachments gain their size.
  const next = BASE.map((m) => (m.id === 'm3'
    ? { ...m, removed: true, removed_at: '2026-11-20T11:00:00Z', text: '', original: undefined, attachments: [] }
    : m.id === 'm1' ? { ...m, attachments: [{ id: 'i', type: 'image/png', width: 40, height: 30,
                                              url: '/chats/media/mail-gateway/' + 'ef'.repeat(16) }] } : m));
  await poll(el, { unread: 1, list_group: 'unread' }, next);
  assert.ok(bubble(el, 'm3').includes('tomb'), 'the tombstone, in its place');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'm4', 'ROW'], 'nothing moved, the row stays');
  assert.ok(!el._origOpen.has('m3') && !el._origCache.has('m3'), 'the open Original went with the mail');
  assert.ok(bubble(el, 'm1').includes('<img class="att-img"'), 'the attachment now renders');
  assert.notEqual(el._thread.node('m1'), kept.m1);
  assert.equal(el._thread.node('m2'), kept.m2, 'untouched bubbles are the same nodes');
  assert.equal(el._thread.node('m4'), kept.m4);
  assert.equal(el._thread.scrollTop, 200, 'the scroll position is kept');
  // A reader at the bottom stays at the bottom.
  el._thread.scrollTop = 500;
  await poll(el, { unread: 1, list_group: 'unread' }, next.map((m) => (m.id === 'm4' ? { ...m, text: 'edited' } : m)));
  assert.equal(el._thread.scrollTop, 1000);
  assert.ok(bubble(el, 'm4').includes('edited'));
});

await ok('can_send false: the server\'s reason in place of composer, chips and draft', async () => {
  const el = await open({ draft: { text: 'staged', author: 'agent', version: 2 } });
  const c = el._chatComposerHtml();
  assert.ok(c.includes('<p class="cannot-send">Mail is read-only &lt;for now&gt;.</p>'), c);
  assert.ok(c.includes('data-note'), 'the note line stays for Mark as read');
  for (const gone of ['data-composer=', 'qchip', '<textarea', 'draft-tag', 'to-line', 'data-mic']) {
    assert.ok(!c.includes(gone), `no ${gone}`);
  }
  assert.equal(el._gripHtml(), '', 'no grip');
  assert.equal(el._draft, '', 'the draft is not adopted');
  await poll(el, { draft: { text: 'newer', author: 'agent', version: 5 } }, BASE);
  assert.equal(el._draft, '', 'nor a newer one');
  el._leaving();
  assert.ok(!log.some((r) => r.url.endsWith('/draft')), 'nothing saved');
  // No reason given: a line of its own.
  el._chat.cannot_send_reason = undefined;
  assert.ok(el._chatComposerHtml().includes('<p class="cannot-send">Nothing can be sent from here in this chat.</p>'));
  // The switch flips: the page renders anew with a composer.
  const n = renders;
  await poll(el, { can_send: true }, BASE);
  assert.equal(renders, n + 1);
  assert.ok(el._chatComposerHtml().includes('data-composer="chat"'));
  assert.ok(el._gripHtml().includes('data-grip'));
  assert.ok(el._chatComposerHtml().includes('placeholder="Reply to Ann Example, Bob Builder …"'));
});

await ok('messenger chats are unchanged', async () => {
  const SID = 'signal:~+41000:+41999';
  const sum = {
    id: SID, channel: 'signal', account: '+41000', key: '+41999', name: 'Mara', group: false,
    unread: 0, list_group: 'read', last_read: null, inbound_seq: 1, archived: false, muted: false,
    starred: false, companion: null, instructed: null, draft: { text: 'hello', author: 'agent', version: 1 },
    messages: '/chats/s/messages',
  };
  wire.length = 0;
  const el = new Page();
  el.attachShadow({ mode: 'open' });
  el._id = SID;
  el.render = function () {
    const thread = this._state === 'ok' ? new Thread(this._chatThreadHtml()) : null;
    this.shadowRoot = { querySelector: (s) => (s === '[data-chat-thread]' ? thread : null), querySelectorAll: () => [] };
    this._thread = thread;
  };
  const loading = el._load();
  take((w) => w.url === '/chats', 'the list').answer({ chats: [sum] });
  await settle();
  const msgs = [
    { id: 'a', chat: SID, direction: 'in', text: 'hi\nthere', ts: '2026-11-20T08:00:00Z', sender: '+41999', sender_name: 'Mara' },
    { id: 'b', chat: SID, direction: 'out', author: 'device', text: 'yo', ts: '2026-11-20T08:01:00Z' },
  ];
  take((w) => w.url === '/chats/s/messages', 'the messages').answer({ chat: sum, messages: msgs });
  await loading;
  clearInterval(el._pollTimer);
  const a = el._thread.node('a').html;
  assert.ok(/^<div class="msg in" data-mid="a"><div class="bubble"><span class="txt">hi<br>there<\/span><span class="stamp">[^<]*<\/span><\/div><\/div>$/.test(a), a);
  assert.ok(el._thread.node('b').html.includes('<small class="who">You &middot; phone</small>'));
  assert.ok(el._headHtml().includes('<small class="head-sub">via +41000</small>'));
  assert.equal(el._draft, 'hello', 'the draft is adopted');
  assert.ok(el._chatComposerHtml().includes('data-composer="chat"') && el._chatComposerHtml().includes('qchip'));
  assert.ok(el._chatComposerHtml().includes('placeholder="Message Mara …"'));
  assert.ok(el._gripHtml().includes('data-grip'));
  // A messenger group's labels, as before.
  el._chat.group = true;
  assert.ok(el._chatMsgHtml(msgs[0]).includes('>Mara</small>'));
  assert.ok(!el._chatMsgHtml({ ...msgs[0], sender_name: null }).includes('class="who sender"'), 'no address fallback off mail');
  assert.ok(!el._chatMsgHtml({ ...msgs[0], subject: 'X' }).includes('msg-subj'), 'subjects are mail only');
});

await ok('the reference documents carry a mail thread that renders', async () => {
  const doc = JSON.parse(readFileSync(process.env.MAIL_FIXTURE, 'utf-8'));
  const orig0 = JSON.parse(readFileSync(process.env.MAIL_ORIGINAL_FIXTURE, 'utf-8'));
  const list = JSON.parse(readFileSync(process.env.CHATS_FIXTURE, 'utf-8'));
  const sum = list.chats.find((c) => c.channel === 'email');
  assert.ok(sum, 'the list carries the mail chat');
  assert.equal(sum.messages, '/data/chats/email-quarterly.json');
  const row = rowOf(listHtml(list.chats), sum.id);
  assert.ok(row.includes('>Ann Example, Bob Builder, carla@example.net</span>'), row);
  assert.ok(row.includes('<span class="pv-who">Ann:</span>'));
  wire.length = 0;
  const el = new Page();
  el.attachShadow({ mode: 'open' });
  el._id = doc.chat.id;
  el.render = function () {
    const thread = this._state === 'ok' ? new Thread(this._chatThreadHtml()) : null;
    this.shadowRoot = { querySelector: (s) => (s === '[data-chat-thread]' ? thread : null), querySelectorAll: () => [] };
    this._thread = thread;
  };
  const loading = el._load();
  take((w) => w.url === '/chats', 'the list').answer(list);
  await settle();
  take((w) => w.url === sum.messages, 'the messages').answer(doc);
  await loading;
  clearInterval(el._pollTimer);
  const ids = doc.messages.map((m) => m.id);
  assert.deepEqual(el._thread.layout(), [...ids, 'ROW']);
  const html = (i) => el._thread.node(ids[i]).html;
  assert.ok(html(0).includes('<img class="att-img"'), 'the inline image');
  assert.ok(html(0).includes('q3-numbers.xlsx'), 'the sheet, as a file row');
  assert.ok(html(1).includes('You &middot; mail client'));
  assert.ok(html(2).includes('<div class="msg-subj">Re: Quarterly numbers (revised table)</div>'));
  assert.ok(html(3).includes('tomb') && html(3).includes('carla@example.net'));
  for (const i of [0, 1, 2, 4]) assert.ok(html(i).includes('data-orig-toggle'), `Original on ${ids[i]}`);
  assert.ok(!html(3).includes('data-orig-toggle'));
  assert.ok(el._chatComposerHtml().includes('cannot-send'));
  // The original's reference document belongs to the HTML mail.
  const bob = doc.messages.find((m) => m.id === orig0.id);
  assert.equal(orig0.html_url, `${bob.original}.html`);
  el._toggleOriginal(bob.id);
  take((w) => w.url === bob.original, 'the original').answer(orig0);
  await settle();
  await settle();
  assert.equal(el._thread.node(bob.id)._slot.children[0].getAttribute('src'), orig0.html_url);
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def static_checks() -> list[str]:
    """What the source itself must hold to, whether or not Node is here."""
    errors = []
    with open(CHAT_PAGE, encoding="utf-8") as fh:
        src = fh.read()
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("//"))
    # The frame is built by DOM calls; no markup string ever names one.
    if re.search(r"<iframe", code):
        errors.append("chat-page.js builds an <iframe> from markup")
    if "createElement('iframe')" not in code:
        errors.append("chat-page.js no longer builds the Original's frame with createElement")
    if "'allow-popups allow-popups-to-escape-sandbox'" not in code:
        errors.append("the frame's sandbox changed")
    # A full render mounts the frames of what was left open.
    render = re.search(r"\n  render\(\) \{(.*?)\n  \}\n", src, re.S)
    if not render or "this._mountFrames()" not in render.group(1):
        errors.append("render() no longer mounts the open Originals' frames")
    with open(FIXTURE, encoding="utf-8") as fh:
        doc = json.load(fh)
    msgs = doc["messages"]
    if not any(m.get("removed") for m in msgs):
        errors.append("the mail fixture has no tombstone")
    if not any(a.get("type", "").startswith("image/") for m in msgs for a in m.get("attachments") or []):
        errors.append("the mail fixture has no inline image")
    if not doc["chat"].get("group"):
        errors.append("the mail fixture is not a group thread")
    return errors


def main() -> int:
    errors = static_checks()
    if errors:
        for e in errors:
            print("FAIL: " + e)
        return 1
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the mail chats test needs it")
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
        env = dict(os.environ,
                   MAIL_FIXTURE=os.path.abspath(FIXTURE),
                   MAIL_ORIGINAL_FIXTURE=os.path.abspath(ORIGINAL_FIXTURE),
                   CHATS_FIXTURE=os.path.abspath(os.path.join(DATA, "chats.json")))
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True,
                              text=True, timeout=120, env=env)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: mail chats test")
            return 1
    print("PASS: mail chats test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
