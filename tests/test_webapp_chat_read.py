#!/usr/bin/env python3
"""Read state on the chat page (webapp/components/chat-page.js).

Opening a chat no longer reads it; the page shows a Mark as read button
instead. The element is driven under Node with a scripted DOM (a thread that
holds its top-level nodes in order, enough to see where the button sits) and
a scripted fetch, and pins:

  * opening a chat, a poll, coming back to the page and a send post no read —
    the only read requests are the button's and its Undo's, and the source
    holds a single call site of the read endpoint;
  * the button sits under the latest inbound message whenever the chat is
    unread, and not at all when it is read;
  * pressing it posts `{}`, the button gives way to "Marked as read" with
    Undo, and Undo posts `{restore, marked}` from the answer and brings the
    button back; a 409 says there is nothing to undo;
  * each poll takes the chat summary's unread, list group and flags: a newer
    inbound message moves the button under it, a read elsewhere (a reply
    from the phone) takes it away, a starred chat gets its star; a poll that
    left before a read does not bring the old unread state back;
  * the "new" waterline is defined from the unread count delivered at load —
    the last `unread` inbound messages, outbound ones skipped — and stays put;
  * the shelf hears the server's unread count, never "0 because opened".

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.
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

// ── A scripted DOM ───────────────────────────────────────────────────────────
// The page's thread is the one part that matters here: it is kept as an
// ordered list of top-level nodes, each the HTML of one <div> the page put
// there, so a test can ask what sits under which bubble.
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
class Node {
  constructor(thread, html) {
    this.thread = thread;
    this.html = html;
    const open = html.slice(0, html.indexOf('>') + 1);
    this.mid = (/data-mid="([^"]*)"/.exec(open) || [])[1] || null;
    this.isRow = open.includes('data-mark-read-row');
    this.button = this.isRow ? { focus: () => { globalThis.focusedRow = this.html; } } : null;
  }
  getAttribute(n) { return n === 'data-mid' ? this.mid : null; }
  remove() { this.thread.nodes = this.thread.nodes.filter((x) => x !== this); }
  contains(x) { return !!x && x === this.button; }
  replaceWith(n) { const i = this.thread.nodes.indexOf(this); this.thread.nodes[i] = n; n.thread = this.thread; }
  insertAdjacentHTML(where, html) {
    assert.equal(where, 'afterend');
    const i = this.thread.nodes.indexOf(this);
    this.thread.nodes.splice(i + 1, 0, ...chunks(html).map((h) => new Node(this.thread, h)));
  }
}
class Thread {
  constructor(html) { this.nodes = chunks(html).map((h) => new Node(this, h)); this.scrollTop = 0; this.clientHeight = 500; }
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
  // What is in the thread, by kind: a message id, 'ROW' (the button),
  // 'UNDO' (its Undo), 'NEW <n>' (the waterline), days left out.
  layout() {
    return this.nodes.map((n) => (n.mid ? n.mid
      : n.isRow ? (n.html.includes('data-mark-read-undo') ? 'UNDO' : 'ROW')
        : n.html.includes('unread-sep') ? `NEW ${/<span>([^<]*)<\/span>/.exec(n.html)[1]}` : null))
      .filter(Boolean);
  }
}
const registry = new Map();
class FakeElement {
  constructor() { this._attrs = new Map(); this.shadowRoot = null; this.style = {}; this.dataset = {}; }
  attachShadow() { this.shadowRoot = { querySelector() { return null; }, querySelectorAll() { return []; } }; return this.shadowRoot; }
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
  documentElement: { dataset: {} }, title: 'Retinue — Chat', activeElement: null,
};
globalThis.window = globalThis;
globalThis.innerHeight = 800;
globalThis.location = { hash: '', pathname: '/chat.html', search: '?id=c1', href: 'http://dash/chat.html?id=c1' };
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
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
const settle = () => new Promise((r) => setTimeout(r, 0));
const take = (pred, what) => {
  const i = wire.findIndex(pred);
  assert.ok(i >= 0, `expected ${what}, saw ${JSON.stringify(wire.map((w) => [w.method, w.url]))}`);
  return wire.splice(i, 1)[0];
};
const reads = () => log.filter((r) => /\/read$/.test(r.url));

await import('./chat-page.js');
const Page = registry.get('retinue-chat-page');
const { shelf } = await import('./shelf-store.js');
const observed = [];
shelf.observe = (key, obs) => observed.push([key, obs]);

const ID = 'signal:~+41000:+41999';
const MSG_URL = '/chats/x/messages';
const msg = (id, direction, ts, extra = {}) => ({ id, chat: ID, direction, text: id, ts, ...extra });
const summary = (extra = {}) => ({
  id: ID, channel: 'signal', account: '+41000', key: '+41999', name: 'Mara', group: false,
  unread: 0, list_group: 'read', last_read: null, archived: false, muted: false, starred: false,
  companion: null, instructed: null, draft: null, messages: MSG_URL, ...extra,
});
const BASE = [
  msg('m1', 'in', '2026-11-20T08:00:00Z'),
  msg('m2', 'out', '2026-11-20T08:05:00Z', { author: 'user' }),
  msg('m3', 'in', '2026-11-20T09:00:00Z'),
  msg('m4', 'out', '2026-11-20T09:01:00Z', { author: 'agent' }),
  msg('m5', 'in', '2026-11-20T09:02:00Z'),
];

// A page loaded the way chat.html loads it: the list, then the chat's own
// message document. render() is the page's full rebuild; here it only builds
// the thread, which is the part under test.
async function open(chat, messages = BASE) {
  wire.length = 0;
  log.length = 0;
  observed.length = 0;
  const el = new Page();
  el.attachShadow({ mode: 'open' });
  el._id = ID;
  el.render = function () {
    const thread = this._state === 'ok' ? new Thread(this._chatThreadHtml()) : null;
    this.shadowRoot = { querySelector: (s) => (s === '[data-chat-thread]' ? thread : null), querySelectorAll: () => [] };
    this._thread = thread;
  };
  const loading = el._load();
  take((w) => w.url === '/chats', 'the list').answer({ chats: [summary(chat)] });
  await settle();
  take((w) => w.url === MSG_URL, 'the messages').answer({ chat: summary(chat), messages: messages.map((m) => ({ ...m })) });
  await loading;
  clearInterval(el._pollTimer);  // the test polls by hand
  return el;
}
async function poll(el, chat, messages) {
  const p = el._poll();
  take((w) => w.url === MSG_URL, 'the poll').answer({ chat: summary(chat), messages: messages.map((m) => ({ ...m })) });
  await p;
}

let passed = 0;
async function ok(name, fn) { await fn(); passed += 1; console.log('  ok  ' + name); }

await ok('opening an unread chat posts no read; the button sits under the latest inbound message', async () => {
  const el = await open({ unread: 1, list_group: 'unread', last_read: '2026-11-20T09:01:30Z' });
  assert.deepEqual(reads(), [], 'no read on open');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'm4', 'NEW 1 new', 'm5', 'ROW']);
  assert.ok(el._thread.nodes.find((n) => n.isRow).html.includes('<button type="button" class="mark-read" data-mark-read'),
    'a real button');
  assert.ok(el._thread.nodes.find((n) => n.isRow).html.includes('Mark as read</button>'), 'labelled in words');
  // Coming back to the page polls, and reads nothing either.
  el._startPolling();
  clearInterval(el._pollTimer);
  el._onVis();
  take((w) => w.url === MSG_URL, 'the catch-up poll').answer({ chat: summary({ unread: 1, list_group: 'unread' }), messages: BASE });
  await settle();
  assert.deepEqual(reads(), []);
  document.removeEventListener('visibilitychange', el._onVis);
});

await ok('the shelf hears the server\'s count, not "0 because opened"', async () => {
  const el = await open({ unread: 2, list_group: 'unread' });
  const load = observed.find(([, o]) => 'unread' in o);
  assert.deepEqual([load[0], load[1].unread], [`chat:${ID}`, 2]);
  el._mirrorLocation = () => null;  // a layout measure, not what this is about
  assert.equal(el._shelfItem().unread, 2);
});

await ok('a read chat shows no button and no waterline', async () => {
  const el = await open({ unread: 0, list_group: 'read' });
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'm3', 'm4', 'm5']);
});

await ok('the waterline counts inbound messages from the end, outbound ones skipped', async () => {
  const el = await open({ unread: 2, list_group: 'unread' });
  // m3 and m5 are the last two inbound; m4 (Ara's) between them is not counted.
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'NEW 2 new', 'm3', 'm4', 'm5', 'ROW']);
  const more = await open({ unread: 9, list_group: 'unread' });
  assert.deepEqual(more._thread.layout().slice(0, 2), ['NEW 9 new', 'm1'], 'older history: above the first');
  const groupOnly = await open({ unread: undefined, list_group: 'unread' });
  assert.deepEqual(groupOnly._thread.layout(), ['m1', 'm2', 'm3', 'm4', 'NEW New', 'm5', 'ROW'],
    'a group without a count still says unread');
});

await ok('Mark as read posts {} and offers Undo; Undo restores the previous mark and the button', async () => {
  const el = await open({ unread: 2, list_group: 'unread', last_read: '2026-11-20T08:30:00Z' });
  const p = el._markRead();
  const post = take((w) => w.method === 'POST', 'the read');
  assert.equal(post.url, `/chats/${encodeURIComponent(ID)}/read`);
  assert.deepEqual(post.body, {});
  post.answer({ id: ID, last_read: '2026-11-20T09:02:00Z', previous: '2026-11-20T08:30:00Z', unread: 0, list_group: 'read' });
  await p;
  assert.equal(el._chat.unread, 0);
  assert.equal(el._chat.list_group, 'read');
  assert.deepEqual(el._thread.layout(), ['m1', 'm2', 'NEW 2 new', 'm3', 'm4', 'm5', 'UNDO'],
    'the waterline stays; the button gives way to its Undo');
  assert.ok(el._thread.nodes.find((n) => n.isRow).html.includes('Marked as read'));
  assert.ok(observed.some(([, o]) => o.unread === 0), 'the shelf hears it');
  const u = el._undoMarkRead();
  const undo = take((w) => w.method === 'POST', 'the undo');
  assert.deepEqual(undo.body, { restore: '2026-11-20T08:30:00Z', marked: '2026-11-20T09:02:00Z' });
  undo.answer({ id: ID, last_read: '2026-11-20T08:30:00Z', unread: 2, list_group: 'unread' });
  await u;
  assert.equal(el._chat.unread, 2);
  assert.deepEqual(el._thread.layout().slice(-2), ['m5', 'ROW'], 'the button is back');
});

await ok('focus follows the row: from the button to Undo and back', async () => {
  const el = await open({ unread: 1, list_group: 'unread' });
  const row = () => el._thread.nodes.find((n) => n.isRow);
  document.activeElement = row().button;
  globalThis.focusedRow = '';
  const p = el._markRead();
  take((w) => w.method === 'POST', 'the read').answer({ id: ID, last_read: 'L', previous: null, unread: 0, list_group: 'read' });
  await p;
  assert.ok(globalThis.focusedRow.includes('data-mark-read-undo'), 'focus on Undo');
  document.activeElement = row().button;
  const u = el._undoMarkRead();
  take((w) => w.method === 'POST', 'the undo').answer({ id: ID, last_read: null, unread: 1, list_group: 'unread' });
  await u;
  assert.ok(globalThis.focusedRow.includes('data-mark-read'), 'and on the button again');
  assert.ok(!globalThis.focusedRow.includes('data-mark-read-undo'));
  document.activeElement = null;
});

await ok('an Undo the mark has outlived (409) says so and leaves the chat read', async () => {
  const el = await open({ unread: 1, list_group: 'unread' });
  let note = '';
  el._showNote = (h) => { note = h; };
  const p = el._markRead();
  take((w) => w.method === 'POST', 'the read').answer({ id: ID, last_read: 'L', previous: 'P', unread: 0, list_group: 'read' });
  await p;
  const u = el._undoMarkRead();
  take((w) => w.method === 'POST', 'the undo').answer({ error: 'moved' }, 409);
  await settle();
  take((w) => w.url === MSG_URL, 'the poll after it').answer({ chat: summary({ unread: 0, list_group: 'read' }), messages: BASE });
  await u;
  assert.ok(note.includes('nothing to undo'), note);
  assert.deepEqual(el._thread.layout().slice(-1), ['m5'], 'no button, no Undo');
});

await ok('a server without `previous` reads, and offers no Undo', async () => {
  const el = await open({ unread: 1, list_group: 'unread' });
  const p = el._markRead();
  take((w) => w.method === 'POST', 'the read').answer({ id: ID, last_read: 'L' });
  await p;
  assert.deepEqual(el._thread.layout().slice(-1), ['m5']);
  assert.equal(el._chat.list_group, 'read');
});

await ok('a newer inbound message moves the button under it, and ends an Undo', async () => {
  const el = await open({ unread: 1, list_group: 'unread' });
  const p = el._markRead();
  take((w) => w.method === 'POST', 'the read').answer({ id: ID, last_read: 'L', previous: 'P', unread: 0, list_group: 'read' });
  await p;
  const next = [...BASE, msg('m6', 'out', '2026-11-20T10:00:00Z', { author: 'agent' }), msg('m7', 'in', '2026-11-20T10:05:00Z')];
  await poll(el, { unread: 1, list_group: 'unread' }, next);
  assert.equal(el._readUndo, null, 'nothing left to undo');
  assert.deepEqual(el._thread.layout().slice(-3), ['m6', 'm7', 'ROW']);
  assert.equal(el._thread.layout().filter((x) => x === 'ROW' || x === 'UNDO').length, 1, 'one row only');
});

await ok('a reply from elsewhere, seen on the next poll, takes the button away', async () => {
  const el = await open({ unread: 1, list_group: 'unread' });
  const next = [...BASE, msg('m6', 'out', '2026-11-20T10:00:00Z', { author: 'device' })];
  await poll(el, { unread: 0, list_group: 'read', last_read: '2026-11-20T10:00:00Z' }, next);
  assert.deepEqual(el._thread.layout().slice(-2), ['m5', 'm6']);
  assert.equal(el._chat.last_read, '2026-11-20T10:00:00Z');
  assert.deepEqual(reads(), []);
});

await ok('the poll takes the flags from the server', async () => {
  const el = await open({ unread: 0, list_group: 'read' });
  let redrawn = 0;
  el._refreshFlagsUi = () => { redrawn += 1; };
  await poll(el, { starred: true, archived: true, list_group: 'archived' }, BASE);
  assert.equal(el._chat.starred, true);
  assert.equal(el._chat.archived, true);
  assert.equal(el._chat.list_group, 'archived');
  assert.equal(redrawn, 1);
  await poll(el, { starred: true, archived: true, list_group: 'archived' }, BASE);
  assert.equal(redrawn, 1, 'unchanged flags are not redrawn');
});

await ok('a poll that left before a read does not bring the unread state back', async () => {
  const el = await open({ unread: 1, list_group: 'unread' });
  const stale = el._poll();
  const pollReq = take((w) => w.url === MSG_URL, 'the poll');
  const p = el._markRead();
  take((w) => w.method === 'POST', 'the read').answer({ id: ID, last_read: 'L', previous: 'P', unread: 0, list_group: 'read' });
  await p;
  pollReq.answer({ chat: summary({ unread: 1, list_group: 'unread' }), messages: BASE });
  await stale;
  assert.equal(el._chat.unread, 0);
  assert.deepEqual(el._thread.layout().slice(-1), ['UNDO']);
});

await ok('a send reads the chat server-side: the page posts no read and drops the button', async () => {
  const el = await open({ unread: 1, list_group: 'unread' });
  el._refreshChatComposer = () => {};
  el._keepLocal = () => {};
  const p = el._sendChat('hello');
  await settle();
  const send = take((w) => w.method === 'POST' && w.url.endsWith('/send'), 'the send');
  send.answer({ id: 'm9', chat: ID, direction: 'out', author: 'user', text: 'hello', ts: '2026-11-20T11:00:00Z' });
  await p;
  assert.deepEqual(reads(), [], 'no read after a send');
  assert.equal(el._chat.unread, 0);
  assert.ok(!el._thread.layout().includes('ROW'));
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    # One read call site: the page posts a read only for Mark as read and its
    # Undo, both through _postReadMark — none on open, poll, visibility, the
    # draft's shrink or a send.
    with open(CHAT_PAGE, encoding="utf-8") as fh:
        src = fh.read()
    sites = re.findall(r"/read`", src)
    if len(sites) != 1 or "_postRead(" in src:
        print("FAIL: chat-page.js must post a read from _postReadMark alone")
        return 1
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the chat read test needs it")
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
            print("FAIL: chat read test")
            return 1
    print("PASS: chat read test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
