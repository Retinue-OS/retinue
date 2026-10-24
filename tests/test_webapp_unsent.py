#!/usr/bin/env python3
"""Behavioural tests for unsent input across page loads
(webapp/components/drafts.js, webapp/components/dictation.js, the "Dictation"
section of webapp/sw.js, and their use in conversation.js and
project-page.js).

Leaving a page is never held back by what it holds: a tab of the navigation
row, a Home press of the Android shell or a back gesture simply goes. What
the user put in waits where they left it instead. These checks pin that
under Node, with two copies of the components standing for two consecutive
pages of one device — one localStorage, one IndexedDB, one service worker
(the real sw.js, run in a vm context):

- a typed draft is on the device as it is typed, and in its box on the next
  page; picked files come back with it; a draft nobody came back to within
  a month is dropped;
- a recording still running when the page goes is handed to the worker with
  what it holds, and its transcript is in the box on the next page;
- a transcription still running when the page goes is finished by the
  worker, and lands in the box on the next page — whether that page opens
  before or after it is done;
- the page that recorded a dictation and is still there gets it, once; a
  second tab on the same place takes nothing that is not its own;
- a worker that does not take a job up (an older version) leaves it to the
  page's own fetch, and the job never comes back twice;
- a send still on the wire when the page goes is a keepalive request, kept
  in the draft and settled on the next page: dropped once the thread holds
  it, left in the box with a note when it does not — a first message, which
  has no thread to read, by the thread it opened; keepalive alone is never
  taken for delivery; a send that completed leaves no marker behind;
- a composer with no thread keeps its own draft per host (a chat's companion
  never shows the home composer's words);
- a page frozen in the back-forward cache whose dictation another page took
  meanwhile settles that job as taken elsewhere and shows the device's draft;
- a project's command bar and an unsaved edit of its file come back, the edit
  over the version it was started from, so a save still detects a conflict;
  back while editing leaves the page and keeps the edit, it no longer drops it.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.join(HERE, "..", "webapp")
COMPONENTS = os.path.join(WEBAPP, "components")

HARNESS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// ── One device: localStorage and IndexedDB, shared by every page ─────────────
const ls = new Map();
globalThis.localStorage = {
  get length() { return ls.size; },
  key: (i) => [...ls.keys()][i] ?? null,
  getItem: (k) => (ls.has(k) ? ls.get(k) : null),
  setItem: (k, v) => { ls.set(k, String(v)); },
  removeItem: (k) => { ls.delete(k); },
};
globalThis.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };

// Just enough IndexedDB: databases of object stores (inline keyPath or
// out-of-line keys), get/put/delete/getAll/openCursor, transactions that
// complete once their last request has. Records are kept by shallow copy.
class FakeTx {
  constructor(store) {
    this.store = store; this.pending = 0; this.done = false;
    this.oncomplete = null; this.onerror = null; this.onabort = null;
    setTimeout(() => this.check(), 0);
  }
  objectStore() { return new FakeStore(this); }
  req(fn) {
    const r = { result: undefined, onsuccess: null };
    this.pending += 1;
    setTimeout(() => {
      r.result = fn();
      if (r.onsuccess) r.onsuccess({ target: r });
      this.pending -= 1;
      this.check();
    }, 0);
    return r;
  }
  check() {
    if (this.pending || this.done) return;
    this.done = true;
    setTimeout(() => { if (this.oncomplete) this.oncomplete(); }, 0);
  }
}
class FakeStore {
  constructor(tx) { this.tx = tx; this.s = tx.store; }
  get(k) { return this.tx.req(() => { const v = this.s.data.get(k); return v === undefined ? undefined : { ...v }; }); }
  getAll() { return this.tx.req(() => [...this.s.data.values()].map((v) => ({ ...v }))); }
  put(v, k) { return this.tx.req(() => { const key = this.s.keyPath ? v[this.s.keyPath] : k; this.s.data.set(key, { ...v }); return key; }); }
  delete(k) { return this.tx.req(() => { this.s.data.delete(k); }); }
  openCursor() {
    const keys = [...this.s.data.keys()];
    let i = 0;
    const r = { result: null, onsuccess: null };
    const step = () => {
      this.tx.pending += 1;
      setTimeout(() => {
        if (i < keys.length) {
          const k = keys[i++];
          r.result = { key: k, value: { ...this.s.data.get(k) }, delete: () => this.s.data.delete(k), continue: step };
        } else {
          r.result = null;
        }
        if (r.onsuccess) r.onsuccess({ target: r });
        this.tx.pending -= 1;
        this.tx.check();
      }, 0);
    };
    step();
    return r;
  }
}
const idbs = new Map();
const indexedDB = {
  open(name, version) {
    const r = { result: null, onsuccess: null, onerror: null, onupgradeneeded: null };
    setTimeout(() => {
      let db = idbs.get(name);
      const upgrade = !db || db.version < version;
      if (!db) { db = { version: 0, stores: new Map() }; idbs.set(name, db); }
      r.result = {
        createObjectStore: (n, o = {}) => { db.stores.set(n, { keyPath: o.keyPath || null, data: new Map() }); },
        transaction: (n) => new FakeTx(db.stores.get(n)),
        close() {},
      };
      if (upgrade) { db.version = version; if (r.onupgradeneeded) r.onupgradeneeded(); }
      if (r.onsuccess) r.onsuccess();
    }, 0);
    return r;
  },
};
globalThis.indexedDB = indexedDB;
const jobs = () => [...((idbs.get('retinue-dictation') || { stores: new Map() }).stores.get('jobs') || { data: new Map() }).data.values()];

// ── Pages: each with its own window, document listeners and client ───────────
const pages = {};
let cur = null;
const mkReg = () => {
  const m = new Map();
  return {
    on: (t, f) => { if (!m.has(t)) m.set(t, []); m.get(t).push(f); },
    off: (t, f) => { m.set(t, (m.get(t) || []).filter((g) => g !== f)); },
    fire: (t, ev = {}) => { for (const f of [...(m.get(t) || [])]) f({ type: t, ...ev }); },
  };
};
let swPost = null;
const clients = new Map();
function page(name) {
  if (!pages[name]) {
    const p = { name, win: mkReg(), doc: mkReg(), alive: true, listeners: [] };
    p.client = { id: `client-${name}`, postMessage: (data) => setTimeout(() => {
      if (p.alive) p.listeners.forEach((f) => f({ data }));
    }, 0) };
    p.navigator = {
      userAgent: 'node', mediaDevices: null,
      serviceWorker: {
        // The source is the client that posted, as of the post: a message sent
        // as the page unloads still names it.
        controller: { postMessage: (data, ports) => { const src = p.alive ? p.client : null; setTimeout(() => swPost(data, src, ports), 0); } },
        addEventListener: (t, f) => { if (t === 'message') p.listeners.push(f); },
      },
    };
    clients.set(p.client.id, p.client);
    pages[name] = p;
  }
  cur = pages[name];
  return cur;
}
// The page goes: pagehide, and its client is no more.
function leave(p) {
  page(p.name);
  p.win.fire('pagehide', { persisted: false });
  p.alive = false;
  clients.delete(p.client.id);
}
// Into the back-forward cache (pagehide, the page kept), and back out of it.
function freeze(p) { page(p.name); p.win.fire('pagehide', { persisted: true }); }
function thaw(p) { page(p.name); p.win.fire('pageshow', { persisted: true }); }
Object.defineProperty(globalThis, 'navigator', { get: () => cur.navigator, configurable: true });
globalThis.addEventListener = (t, f) => cur.win.on(t, f);
globalThis.removeEventListener = (t, f) => cur.win.off(t, f);

// ── A scripted DOM (as in the send-deadline test) ────────────────────────────
const registry = new Map();
class FakeShadow {
  constructor() { this.innerHTML = ''; this.activeElement = null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
class FakeElement {
  constructor() { this._attrs = new Map(); this._l = new Map(); this.shadowRoot = null; this.isConnected = true; this.style = {}; }
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
  removeAttribute(n) { this._attrs.delete(n); }
  toggleAttribute(n, on) {
    const want = on === undefined ? !this.hasAttribute(n) : !!on;
    if (want) this._attrs.set(n, ''); else this._attrs.delete(n);
    return want;
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(`${cur.name}:${n}`, c), get: (n) => registry.get(`${cur.name}:${n}`) };
globalThis.document = {
  addEventListener: (t, f) => cur.doc.on(t, f), removeEventListener: (t, f) => cur.doc.off(t, f),
  createElement: () => new FakeElement(),
  querySelector() { return null; }, getElementById() { return null; },
  hidden: false, visibilityState: 'visible', body: { appendChild() {} },
  documentElement: { dataset: {} }, referrer: '',
};
globalThis.window = globalThis;
globalThis.innerHeight = 800;
globalThis.location = { hash: '', pathname: '/', search: '', href: 'http://dash/', origin: 'http://dash', assign() {} };
globalThis.history = { length: 1, pushState() {}, replaceState() {}, back() {} };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};

const tick = () => new Promise((r) => setTimeout(r, 0));
async function until(cond, what) {
  for (let i = 0; i < 400 && !cond(); i++) await tick();
  assert.ok(cond(), what);
}
// The same against the clock, for what waits on a timer of its own.
async function within(ms, cond, what) {
  const end = Date.now() + ms;
  while (!cond() && Date.now() < end) await new Promise((r) => setTimeout(r, 20));
  assert.ok(cond(), what);
}

// ── The gateway, as the pages see it ─────────────────────────────────────────
const A = 'a'.repeat(32), B = 'b'.repeat(32), C = 'c'.repeat(32), D = 'd'.repeat(32), E = 'e'.repeat(32);
const F = 'f'.repeat(32), G = '0'.repeat(32), H = '1'.repeat(32), L = '2'.repeat(32), M = '3'.repeat(32), N = '4'.repeat(32);
const threads = {};
for (const id of [A, B, C, D, E, F, G, H, L, M, N]) {
  threads[id] = { id, title: 'T', pending: false, model: '', messages: [
    { role: 'user', text: 'Earlier', ts: '2026-10-16T15:40:00Z' },
    { role: 'assistant', text: 'Answer', ts: '2026-10-16T15:41:00Z' },
  ] };
}
const posts = [];
let land = false;
let resolveSends = false;   // a reply POST answers with the thread (else it hangs)
let listed = [];            // GET /conversations
const pageTranscribes = [];
const never = () => new Promise(() => {});
const answer = (body, status = 200) => Promise.resolve({ ok: status < 300, status, json: async () => JSON.parse(JSON.stringify(body)) });
let project = null;
const projectSaves = [];
globalThis.fetch = (url, opts = {}) => {
  const method = opts.method || 'GET';
  const read = /^\/conversations\/([0-9a-f]{32})$/.exec(url);
  if (read && method === 'GET') return answer(threads[read[1]]);
  const reply = /^\/conversations\/([0-9a-f]{32})\/messages$/.exec(url);
  if (reply && method === 'POST') {
    const body = JSON.parse(opts.body);
    posts.push({ url, body, keepalive: !!opts.keepalive });
    if (land) threads[reply[1]].messages.push({ role: 'user', text: body.message.trim(), ts: new Date().toISOString() });
    return resolveSends ? answer(threads[reply[1]]) : never();
  }
  if (url === '/conversations' && method === 'POST') { posts.push({ url, body: JSON.parse(opts.body), keepalive: !!opts.keepalive }); return never(); }
  if (url === '/conversations' && method === 'GET') return listed ? answer({ conversations: listed }) : answer({}, 502);
  if (url.startsWith('/conversations/transcribe')) { pageTranscribes.push(url); return answer({ text: 'Vom Seitenabruf.' }); }
  if (url === '/conversation-models') return answer({ models: [] });
  if (url.startsWith('/projects/item') && method === 'GET') return answer(project);
  if (url === '/projects/item' && method === 'POST') {
    const body = JSON.parse(opts.body);
    projectSaves.push(body);
    return body.base_sha === project.sha256 ? answer({ sha256: 'new' }) : answer({ sha256: project.sha256 }, 409);
  }
  if (url.startsWith('/conversations?')) return answer({ conversations: [] });
  return answer({});
};

// ── The service worker: the real sw.js, in a context of its own ──────────────
// Its transcription requests are held until the test answers them.
const swFetches = [];
let swIgnoresDictation = false;
const swMessage = [];
const sandbox = {
  self: {
    addEventListener: (t, f) => { if (t === 'message') swMessage.push(f); },
    clients: { get: async (id) => clients.get(id), claim: async () => {}, matchAll: async () => [] },
    registration: { showNotification: async () => {} }, skipWaiting: () => {},
  },
  caches: { open: async () => ({ addAll: async () => {}, match: async () => null, put: async () => {} }),
            keys: async () => [], match: async () => null, delete: async () => true },
  indexedDB, Blob, AbortController, URL, console, setTimeout, clearTimeout,
  location: { origin: 'http://dash' },
  fetch: (url, opts = {}) => new Promise((resolve, reject) => {
    swFetches.push({ url, opts, answer: (text) => resolve({ ok: true, status: 200, json: async () => ({ text }) }), fail: () => reject(new Error('502')) });
  }),
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('./sw.js', 'utf8'), sandbox);
swPost = (data, source, ports) => {
  if (swIgnoresDictation && data && String(data.type).startsWith('dictation')) return;
  for (const f of swMessage) f({ data, source, ports: ports || [], waitUntil() {} });
};

// A month-old draft, before any page has loaded.
ls.set('retinue-draft:conv:stale', JSON.stringify({ v: { text: 'old' }, at: Date.now() - 40 * 24 * 3600 * 1000 }));

page('one');
const one = {
  drafts: await import('./one/drafts.js'),
  dictation: await import('./one/dictation.js'),
};
await import('./one/conversation.js');
await import('./one/project-page.js');

async function mount(p, id) {
  page(p.name);
  const Conv = customElements.get('retinue-conversation');
  const el = new Conv();
  el._attrs.set('conversation-id', id);
  el._attrs.set('no-autofocus', '');
  el.connectedCallback();
  await until(() => el.thread, 'the thread is read');
  return el;
}
// A composer with no thread yet: plain, or a host's (create-url).
function mountNew(p, attrs = {}) {
  page(p.name);
  const el = new (customElements.get('retinue-conversation'))();
  el._attrs.set('no-autofocus', '');
  for (const [k, v] of Object.entries(attrs)) el._attrs.set(k, v);
  el.connectedCallback();
  return el;
}
// What the composer's box holds, or null while a job owns the row.
const draftText = (el) => { el.render(); const m = el.shadowRoot.innerHTML.match(/<textarea[^>]*>([\s\S]*?)<\/textarea>/); return m ? m[1] : null; };
const stored = (key) => { const raw = ls.get(`retinue-draft:${key}`); return raw ? JSON.parse(raw).v : null; };
const recording = (el, key, words) => {
  el._recState = 'recording';
  el._recKey = key;
  el._recChunks = [new Blob([words || 'audio'], { type: 'audio/webm' })];
  el._mediaRecorder = { mimeType: 'audio/webm', state: 'recording', stop() { this.state = 'inactive'; } };
  el._micHold(true);
};

let passed = 0;
async function ok(name, fn) { await fn(); passed++; console.log('  ok  ' + name); }

await ok('a draft nobody came back to within a month is dropped', () => {
  assert.ok(!ls.has('retinue-draft:conv:stale'));
  one.drafts.saveDraft('x', { text: 'kept' });
  assert.deepEqual(one.drafts.loadDraft('x'), { text: 'kept' });
  one.drafts.saveDraft('x', null);
  assert.equal(one.drafts.loadDraft('x'), null, 'null removes it');
});

// Page one: a thread with words typed, one with files, a recording live, a
// transcription under way, a send on the wire.
const a1 = await mount(page('one'), A);
a1.fill('Noch nicht gesendet');
await ok('a draft is on the device as it is written, before anything leaves', () => {
  assert.equal(stored(`conv:${A}`).text, 'Noch nicht gesendet');
});

const b1 = await mount(page('one'), B);
await b1._addFiles([{ name: 'plan.pdf', type: 'application/pdf', size: 3, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer }]);
await until(() => idbs.get('retinue-drafts') && idbs.get('retinue-drafts').stores.get('files').data.has(`conv:${B}`), 'the file is kept');

const c1 = await mount(page('one'), C);
recording(c1, C, 'live');

const d1 = await mount(page('one'), D);
d1._recChunks = [new Blob(['audio'], { type: 'audio/webm' })];
d1._mediaRecorder = { mimeType: 'audio/webm' };
d1._recIntent = 'review';
const dJob = d1._onRecordingStopped(D, null, '', null);
await until(() => swFetches.length === 1, 'the worker transcribes the dictation');
assert.match(swFetches[0].url, new RegExp(`^/conversations/transcribe\\?thread=${D}$`), 'with its thread as context');

const e1 = await mount(page('one'), E);
land = true;
e1.fill('Kommt das an?');
e1._send('Kommt das an?');
await until(() => posts.length === 1, 'the send is on the wire');
const f1 = await mount(page('one'), F);
land = false;
f1.fill('Und das?');
f1._send('Und das?');
await until(() => posts.length === 2, 'the second send is on the wire');

await ok('a small send goes out as a keepalive request, which outlives the page', () => {
  assert.ok(posts.every((p) => p.keepalive));
});

// The user leaves. Nothing holds them.
leave(pages.one);
await until(() => swFetches.length === 2, 'the live recording went to the worker');

await ok('a live recording is handed on with what it holds, its owner none', () => {
  const live = jobs().find((j) => j.key === `conv:${C}`);
  assert.ok(live && live.owner === '', 'a job of nobody, for whoever opens the thread');
  assert.equal(c1._recState === 'recording' && !c1._recAborted, false, 'the recorder is stopped');
});
await until(() => jobs().find((j) => j.key === `conv:${D}`)?.owner === '', 'the leaving page released its job');
// Both transcriptions finish after the page is gone.
swFetches[0].answer('Diktat unterwegs.');
swFetches[1].answer('Diktat beim Gehen.');
await until(() => jobs().filter((j) => j.state === 'done').length === 2, 'the worker stored both');

// Page two: the user comes back.
page('two');
await import('./two/conversation.js');
await import('./two/project-page.js');
const two = { dictation: await import('./two/dictation.js') };

await ok('the typed words are in their box on the next page', async () => {
  const el = await mount(page('two'), A);
  assert.equal(draftText(el), 'Noch nicht gesendet');
});

await ok('picked files come back with their draft', async () => {
  const el = await mount(page('two'), B);
  await until(() => { el.render(); return el.shadowRoot.innerHTML.includes('plan.pdf'); }, 'the file chip is back');
});

await ok('a recording live when the page went is in the box on the next page, transcribed', async () => {
  const el = await mount(page('two'), C);
  await until(() => draftText(el) === 'Diktat beim Gehen.', 'the transcript landed');
  await until(() => !jobs().some((j) => j.key === `conv:${C}`), 'and the worker forgot it once taken');
  assert.equal(stored(`conv:${C}`).text, 'Diktat beim Gehen.', 'kept on the device too');
});

await ok('a transcription under way when the page went lands in the box on the next page', async () => {
  const el = await mount(page('two'), D);
  await until(() => draftText(el) === 'Diktat unterwegs.', 'the transcript landed');
  await until(() => !jobs().some((j) => j.key === `conv:${D}`), 'taken');
});

await ok('a send the thread holds by now leaves the box; one it does not stays, with a note', async () => {
  const e2 = await mount(page('two'), E);
  await until(() => !e2._busy, 'settled');
  assert.equal(draftText(e2), '', 'the thread has it: nothing to send twice');
  const f2 = await mount(page('two'), F);
  // Not found at first: a keepalive request may still be landing, so the
  // thread is read once more a moment later before the words stay.
  assert.ok(f2._busy, 'locked while it is settled');
  await within(5000, () => !f2._busy, 'settled');
  assert.equal(draftText(f2), 'Und das?', 'not in the thread: still in the box');
  assert.ok(f2.shadowRoot.innerHTML.includes('before this message went out'), 'and the composer says so');
});

await ok('a page opened while the worker still transcribes shows it, then takes the text', async () => {
  const el = await mount(page('two'), G);
  page('three');
  // A third page records into G and goes; page two is still on G.
  const p3 = { el: null };
  await import('./three/conversation.js');
  p3.el = await mount(page('three'), G);
  recording(p3.el, G, 'third');
  leave(pages.three);
  await until(() => swFetches.length === 3, 'handed on');
  page('two');
  el.disconnectedCallback();
  const again = await mount(page('two'), G);
  // It was handed on after page two first looked: the periodic resync (or the
  // page coming back to the front) finds it.
  pages.two.doc.fire('visibilitychange');
  await until(() => again.shadowRoot.innerHTML.includes('Transcribing'), 'the box shows the job running');
  swFetches[2].answer('Spät, aber da.');
  await until(() => draftText(again) === 'Spät, aber da.', 'and then its text');
});

await ok('the page that is still there gets its own dictation, once; a second tab takes nothing', async () => {
  page('two');
  const p = two.dictation.transcribe(`conv:${H}`, new Blob(['x'], { type: 'audio/webm' }), '/conversations/transcribe');
  await until(() => swFetches.length === 4, 'running');
  // Another tab on the same place asks for its jobs: the owner is alive.
  page('four');
  const four = await import('./four/dictation.js');
  const got = [];
  four.adopt(`conv:${H}`, (job) => got.push(job));
  await tick(); await tick(); await tick();
  swFetches[3].answer('Nur einmal.');
  page('two');
  assert.equal(await p, 'Nur einmal.');
  await until(() => !jobs().some((j) => j.key === `conv:${H}`), 'taken');
  assert.equal(got.length, 0, 'the other tab got nothing');
});

await ok('a worker that does not take a job up leaves it to the page, once', async () => {
  page('two');
  swIgnoresDictation = true;
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...rest) => realSetTimeout(fn, ms === 4000 ? 5 : ms, ...rest);
  try {
    const n = pageTranscribes.length;
    const text = await two.dictation.transcribe('conv:old', new Blob(['x'], { type: 'audio/webm' }), '/conversations/transcribe');
    assert.equal(text, 'Vom Seitenabruf.', "the page's own fetch answered");
    assert.equal(pageTranscribes.length, n + 1);
  } finally {
    globalThis.setTimeout = realSetTimeout;
    swIgnoresDictation = false;
  }
});

await ok('a composer with no thread keeps its own draft per host', async () => {
  const home = mountNew(page('two'));
  home.fill('Für die Startseite');
  const companion = mountNew(page('two'), { 'create-url': '/chats/x/companion' });
  assert.equal(companion._key(), 'new:@/chats/x/companion', 'keyed by the thread it would mint');
  assert.equal(draftText(companion), '', "the home composer's words are not in a chat's companion");
  companion.fill('Für den Chat');
  assert.equal(draftText(home), 'Für die Startseite', 'nor the other way round');
  assert.equal(stored('conv:new').text, 'Für die Startseite');
  assert.equal(stored('conv:new:@/chats/x/companion').text, 'Für den Chat');
  home.disconnectedCallback();
  companion.disconnectedCallback();
  // Cleared for the next check, which opens a plain composer of its own.
  one.drafts.saveDraft('conv:new', null);
  one.drafts.saveDraft('conv:new:@/chats/x/companion', null);
});

await ok('a first message left on the wire is looked for by the thread it opened, never assumed sent', async () => {
  page('six');
  await import('./six/conversation.js');
  const c6 = mountNew(page('six'));
  c6.fill('Erste Nachricht');
  c6._send('Erste Nachricht');
  await until(() => posts.some((p) => p.url === '/conversations'), 'the first message is on the wire');
  assert.ok(posts.at(-1).keepalive, 'as a keepalive request');
  leave(pages.six);
  const kept = stored('conv:new');
  assert.ok(kept.sending && kept.sending.keepalive && !kept.sending.target && kept.sending.at, 'marked, with when');
  // The server cannot be asked (offline): the words stay, the note hedges,
  // and the mark stays for the next visit to look again.
  listed = null;
  page('seven');
  await import('./seven/conversation.js');
  const c7 = mountNew(page('seven'));
  await until(() => !c7._busy, 'settled');
  assert.equal(draftText(c7), 'Erste Nachricht', 'still in the box');
  assert.ok(c7.shadowRoot.innerHTML.includes('if it turns up in the thread'), 'with the hedged note');
  assert.ok(stored('conv:new').sending, 'the mark stays');
  leave(pages.seven);
  // Next visit, online: the thread it opened is there, created after the send.
  threads[M].messages.push({ role: 'user', text: 'Erste Nachricht', ts: new Date().toISOString() });
  listed = [{ id: N, initiator: 'user', project: null, created: new Date(Date.now() - 3600 * 1000).toISOString() },
            { id: M, initiator: 'user', project: null, created: new Date().toISOString() }];
  page('eight');
  await import('./eight/conversation.js');
  const c8 = mountNew(page('eight'));
  await until(() => !c8._busy, 'settled');
  assert.equal(draftText(c8), '', 'found: nothing left to send twice');
  assert.ok(!stored('conv:new') || !stored('conv:new').sending, 'and the mark is gone');
  c8.disconnectedCallback();
  listed = [];
});

await ok('a left send that was checked and not found is not checked again', async () => {
  // Page seven's composer above holds the words with the mark; a visit that
  // CAN ask and finds nothing drops the mark — the words stay, once.
  page('ten');
  await import('./ten/conversation.js');
  const c10 = mountNew(page('ten'));
  c10.fill('Zweite Nachricht');
  const before = posts.length;
  c10._send('Zweite Nachricht');
  await until(() => posts.length === before + 1 && posts.at(-1).url === '/conversations', 'on the wire');
  leave(pages.ten);
  listed = [];
  page('eleven');
  await import('./eleven/conversation.js');
  const c11 = mountNew(page('eleven'));
  await until(() => !c11._busy, 'settled');
  assert.equal(draftText(c11), 'Zweite Nachricht', 'in the box');
  assert.ok(c11.shadowRoot.innerHTML.includes('before this message went out'), 'checked: not found');
  assert.ok(!stored('conv:new').sending, 'no mark left to check again');
  c11.disconnectedCallback();
});

await ok('a send that completed leaves no marker behind', async () => {
  resolveSends = true;
  const el = await mount(page('two'), L);
  el.fill('Fertig gesendet');
  await el._send('Fertig gesendet');
  assert.equal(draftText(el), '');
  const kept = stored(`conv:${L}`);
  assert.ok(!kept || !kept.sending, 'a page killed now would not settle a turn that is on no wire');
  resolveSends = false;
  el.disconnectedCallback();
});

await ok('frozen in the back-forward cache, a page whose dictation another page took shows that text', async () => {
  page('nine');
  await import('./nine/conversation.js');
  const k9 = await mount(page('nine'), N);
  k9._recChunks = [new Blob(['audio'], { type: 'audio/webm' })];
  k9._mediaRecorder = { mimeType: 'audio/webm' };
  k9._recIntent = 'review';
  const n = swFetches.length;
  const job = k9._onRecordingStopped(N, null, '', null);
  await until(() => swFetches.length === n + 1, 'the worker transcribes');
  freeze(pages.nine);
  // Its jobs are released as it goes (a navigation takes far longer than
  // the worker needs to hear that). Meanwhile another page shows the thread
  // and takes the transcript.
  await until(() => jobs().some((j) => j.key === `conv:${N}` && j.owner === ''), 'released');
  const k2 = await mount(page('two'), N);
  await until(() => k2.shadowRoot.innerHTML.includes('Transcribing'), 'the other page owns the job now');
  swFetches[n].answer('Aus dem Eis.');
  await until(() => draftText(k2) === 'Aus dem Eis.', 'its text landed there');
  await until(() => !jobs().some((j) => j.key === `conv:${N}`), 'and the worker forgot it');
  // The frozen page comes back: its job is gone, its draft is the device's.
  thaw(pages.nine);
  await job;
  await until(() => draftText(k9) === 'Aus dem Eis.', 'the frozen page shows the text, not an error');
  assert.ok(!k9.shadowRoot.innerHTML.includes("Couldn't transcribe"), 'no error');
  k2.disconnectedCallback();
  k9.disconnectedCallback();
});

await ok("a project's command bar and its unsaved edit come back; the edit keeps its base", async () => {
  project = { id: 'urn:p', title: 'P', path: 'p.md', markdown: '---\ntitle: P\n---\nBody\n', sha256: 'v1' };
  page('two');
  location.search = '?id=urn%3Ap';
  const Page = customElements.get('retinue-project-page');
  const p2 = new Page();
  p2.connectedCallback();
  await until(() => p2._state === 'ok', 'read');
  p2._cmd = 'Mark it done';
  p2._keepCmd();
  p2._startEdit();
  p2._draft = '---\ntitle: P\n---\nBody, edited\n';
  p2._keepEdit();
  // Back while editing goes back, as on any page; the edit is kept, not
  // dropped (the editor's × is the one way to drop it).
  p2._onBack();
  assert.equal(location.href, '/', 'back went back');
  assert.equal(p2._mode, 'edit', 'the editor was not cancelled under the user');
  assert.equal(stored('project-edit:urn:p').text, '---\ntitle: P\n---\nBody, edited\n', 'the edit is kept');
  location.href = 'http://dash/';
  leave(pages.two);
  // Meanwhile the file changes elsewhere.
  project = { ...project, markdown: '---\ntitle: P\n---\nBody (agent)\n', sha256: 'v2' };
  page('five');
  await import('./five/project-page.js');
  const p5 = new (customElements.get('retinue-project-page'))();
  p5.connectedCallback();
  await until(() => p5._state === 'ok', 'read again');
  assert.equal(p5._cmd, 'Mark it done', 'the command waits in its bar');
  assert.equal(p5._mode, 'edit', 'the editor is open again');
  assert.equal(p5._draft, '---\ntitle: P\n---\nBody, edited\n', 'with the words left in it');
  await p5._save();
  assert.equal(projectSaves.at(-1).base_sha, 'v1', 'saved over the version it was started from');
  assert.match(p5._editError, /changed in the background/, 'so the change made meanwhile is a conflict, not lost');
  location.search = '';
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the unsent-input test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        # One copy of the components per page: separate module instances, as
        # two page loads have, over the one device the harness provides.
        for page in ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven"):
            dst = os.path.join(tmp, page)
            os.makedirs(dst)
            for name in os.listdir(COMPONENTS):
                if name.endswith(".js"):
                    shutil.copy(os.path.join(COMPONENTS, name), os.path.join(dst, name))
        shutil.copy(os.path.join(WEBAPP, "sw.js"), os.path.join(tmp, "sw.js"))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True, timeout=120)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stdout.write(proc.stderr)
            print("FAIL: unsent-input behaviour test")
            return 1
    print("PASS: unsent-input behaviour test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
