#!/usr/bin/env python3
"""Behavioural tests for the deadline on a conversation's requests
(webapp/components/conversation.js).

fetch() has no deadline of its own. A send whose request never settled — a
phone that changed networks under it, a WebView that froze it — kept its
composer locked for good: the field and the send button disabled, and every
way off the page held by the unsaved-input guard, which counts a send on the
wire. A chat page's Proofread pressed into that lock only landed its words in
the locked box. These checks pin, under Node with a scripted DOM and a fetch
that never answers:

- a host's turn (a chip of the chat page) shows as a "Sending" bubble while
  it is on the wire, and the same turn pressed again sends nothing;
- past the deadline the send is given up: the composer unlocks, the turn is
  in the box once, and a note says it may still turn up;
- a send whose answer was lost but which the thread holds when it is read
  again counts as delivered — nothing is left to send twice;
- a typed turn that times out keeps its words, once, without a bubble;
- a send that fails outright (host unreachable, a refused status) says so;
- a first turn into a thread the host mints (a chat's companion, which may
  hold that turn already) is checked against what the thread held, both ways;
- a dictation sent off screen gets the same re-read, and a note when kept;
- a kept turn is matched to what was kept, never to the user's own words;
- a read that never answers is given up too, so the poll goes on.

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
// Enough for the element to render into a shadow root and read its own
// markup back; nothing in it is ever found by a selector, so the wiring is
// skipped and the tests drive the element's own methods.
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
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
globalThis.document = {
  addEventListener() {}, removeEventListener() {}, createElement: () => new FakeElement(),
  querySelector() { return null; }, getElementById() { return null; },
  hidden: false, visibilityState: 'visible', body: { appendChild() {} },
  documentElement: { dataset: {} },
};
globalThis.window = globalThis;
globalThis.innerHeight = 800;
globalThis.location = { hash: '', pathname: '/', search: '', href: 'http://dash/' };
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.history = { length: 1, pushState() {}, replaceState() {}, back() {} };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', mediaDevices: null }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };

// ── Time: the deadlines are held for the test to expire ──────────────────────
// Every timer of 30 s or more is a request deadline (the polls run every few
// seconds); those wait in `held` until expire() runs them, everything else
// is a real timer.
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;
const held = [];
globalThis.setTimeout = (fn, ms, ...rest) => {
  if (ms >= 30000) { const t = { fn, deadline: true, cleared: false }; held.push(t); return t; }
  return realSetTimeout(fn, ms, ...rest);
};
globalThis.clearTimeout = (t) => { if (t && t.deadline) t.cleared = true; else realClearTimeout(t); };
const expire = () => { for (const t of held.splice(0)) if (!t.cleared) t.fn(); };
const tick = () => new Promise((r) => realSetTimeout(r, 0));
async function until(cond, what) {
  for (let i = 0; i < 100 && !cond(); i++) await tick();
  assert.ok(cond(), what);
}

// ── The gateway: threads that answer reads, and a reply POST that never does ─
const A = 'a'.repeat(32), B = 'b'.repeat(32), C = 'c'.repeat(32), D = 'd'.repeat(32), E = 'e'.repeat(32);
// F is a chat's companion, minted by the host (idempotent: it may hold turns
// already); G is a thread a dictation is sent into while it is off screen.
const F = 'f'.repeat(32), G = '0'.repeat(32);
const threads = {};
for (const id of [A, B, C, D, E, F, G]) {
  threads[id] = { id, title: 'Judith', pending: false, model: '', messages: [
    { role: 'user', text: 'Earlier question', ts: '2026-10-16T15:40:00Z' },
    { role: 'assistant', text: 'Earlier answer', ts: '2026-10-16T15:41:00Z' },
  ] };
}
const posts = [];
let land = false;        // the gateway stores the turn even though no answer comes back
let hangReads = false;
// How a reply POST fails at once instead of hanging: 'network' (no answer at
// all, as when the browser cannot reach the host) or an HTTP status.
let failWith = null;
// What a dictation transcribes to.
let transcript = '';
const never = (opts) => new Promise((_resolve, reject) => {
  if (opts && opts.signal) opts.signal.addEventListener('abort', () => reject(new Error('AbortError')));
});
const answer = (body) => Promise.resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) });
globalThis.fetch = (url, opts = {}) => {
  const method = opts.method || 'GET';
  const read = /^\/conversations\/([0-9a-f]{32})$/.exec(url);
  if (read && method === 'GET') return hangReads ? never(opts) : answer(threads[read[1]]);
  const reply = /^\/conversations\/([0-9a-f]{32})\/messages$/.exec(url);
  if (reply && method === 'POST') {
    const body = JSON.parse(opts.body);
    posts.push(body);
    if (failWith === 'network') return Promise.reject(new TypeError('Failed to fetch'));
    if (failWith) return Promise.resolve({ ok: false, status: failWith, json: async () => ({}) });
    if (land) threads[reply[1]].messages.push({ role: 'user', text: body.message.trim(), ts: new Date().toISOString() });
    return never(opts);
  }
  if (url === '/conversation-models') return answer({ models: [] });
  if (url === '/chats/x/companion' && method === 'POST') return answer({ id: F });
  if (url.startsWith('/conversations/transcribe') && method === 'POST') return answer({ text: transcript });
  return answer({});
};

await import('./conversation.js');
const Conv = registry.get('retinue-conversation');
assert.ok(Conv, 'the element registered itself');
const mount = async (id) => {
  const el = new Conv();
  el._attrs.set('conversation-id', id);
  el._attrs.set('no-autofocus', '');
  el.connectedCallback();
  await until(() => el.thread, 'the thread is read');
  return el;
};
// What the composer's box holds: the draft, as the last render wrote it.
const draftText = (el) => { el.render(); return el.shadowRoot.innerHTML.match(/<textarea[^>]*>([\s\S]*?)<\/textarea>/)[1]; };
const sendingBubble = (el) => el.shadowRoot.innerHTML.includes('class="msg me sending"');

let passed = 0;
async function ok(name, fn) { await fn(); passed++; console.log('  ok  ' + name); }

await ok("a host's turn shows as going, and the same press again sends nothing", async () => {
  const el = await mount(A);
  const sent = el.ask('Proofread this draft:\n\nNoch nach Wirkung von der Migräne.');
  await until(() => posts.length === 1, 'the turn went on the wire');
  assert.ok(el._busy, 'on the wire: the composer is locked');
  assert.ok(sendingBubble(el), 'and the turn shows as a Sending bubble');
  await el.ask('Proofread this draft:\n\nNoch nach Wirkung von der Migräne.');
  await tick();
  assert.equal(posts.length, 1, 'the repeat press sends nothing');
  assert.ok(!draftText(el).includes('Proofread'), 'nor lands in the locked box');
  expire();
  await sent;
  // The re-read after the timeout has a deadline of its own.
  assert.ok(!el._busy, 'past the deadline the composer unlocks');
  assert.equal(draftText(el).split('Proofread this draft').length - 1, 1, 'the turn is in the box, once');
  assert.match(el._attachError, /No answer from the server/, 'and a note says so');
  assert.ok(!sendingBubble(el), 'the bubble is gone');
});

await ok('a send whose answer was lost, but which the thread holds, counts as delivered', async () => {
  const el = await mount(B);
  land = true;
  const sent = el.ask('Summarize this chat since my last reply.');
  await until(() => posts.length === 2, 'the turn went on the wire');
  expire();
  await sent;
  land = false;
  assert.ok(!el._busy, 'unlocked');
  assert.equal(el._attachError, '', 'no note: it went out');
  assert.ok(!draftText(el).includes('Summarize'), 'nothing is left to send twice');
  const mine = el.thread.messages.filter((m) => m.role === 'user' && m.text === 'Summarize this chat since my last reply.');
  assert.equal(mine.length, 1, 'the thread shows the turn');
});

await ok('a typed turn that times out keeps its words, once, without a bubble', async () => {
  const el = await mount(C);
  el.fill('Hoi Judith');
  const sent = el._send('Hoi Judith');
  await until(() => posts.length === 3, 'the turn went on the wire');
  assert.ok(!sendingBubble(el), 'its words are in the box: no bubble');
  expire();
  await sent;
  assert.ok(!el._busy, 'unlocked');
  assert.equal(draftText(el), 'Hoi Judith', 'the words stay, once');
  assert.match(el._attachError, /No answer from the server/);
});

await ok('a send that fails outright says why, and keeps its words', async () => {
  const el = await mount(E);
  el.fill('Bis morgen');
  failWith = 'network';
  await el._send('Bis morgen');
  assert.ok(!el._busy, 'unlocked');
  assert.equal(draftText(el), 'Bis morgen', 'the words stay');
  assert.match(el._attachError, /Couldn't reach the server/, 'an unreachable host is named, not left silent');
  failWith = 502;
  await el._send('Bis morgen');
  assert.match(el._attachError, /refused the message \(502\)/, 'a refusal names its status');
  failWith = null;
});

await ok("a first turn into a host's thread is checked against what that thread held", async () => {
  // The companion already holds this very turn from an earlier press, so a
  // count from an empty thread would be wrong both ways.
  threads[F].messages.push({ role: 'user', text: 'Proofread my draft before I send it.', ts: '2026-10-17T09:00:00Z' });
  const fresh = () => {
    const el = new Conv();
    el._attrs.set('create-url', '/chats/x/companion');
    el._attrs.set('no-autofocus', '');
    el.connectedCallback();
    return el;
  };
  // Stored, its answer lost: the thread holds it once more — delivered.
  let el = fresh();
  let n = posts.length;
  land = true;
  let sent = el.ask('Proofread my draft before I send it.');
  await until(() => posts.length === n + 1, 'the turn went on the wire');
  expire();
  await sent;
  land = false;
  assert.equal(el._attachError, '', 'the thread holds it once more: it went out');
  assert.ok(!draftText(el).includes('Proofread'), 'nothing is left to send twice');
  // Not stored: the thread holds it no more often than before — kept.
  el = fresh();
  n = posts.length;
  sent = el.ask('Proofread my draft before I send it.');
  await until(() => posts.length === n + 1, 'the turn went on the wire');
  expire();
  await sent;
  assert.match(el._attachError, /No answer from the server/, 'not taken for delivered');
  assert.equal(draftText(el).split('Proofread my draft').length - 1, 1, 'the turn is kept, once');
});

await ok('a dictation sent off screen is checked against the thread, and says why it was kept', async () => {
  const el = await mount(A);
  const dictate = (words) => {
    transcript = words;
    el._recChunks = [new Blob(['audio'], { type: 'audio/webm' })];
    el._recIntent = 'send';
    return el._onRecordingStopped(G, null, '', null);
  };
  // Stored, its answer lost: delivered, nothing left in G's draft.
  let n = posts.length;
  land = true;
  let job = dictate('Ruf mich morgen an.');
  await until(() => posts.length === n + 1, 'the dictation went on the wire');
  expire();
  await job;
  land = false;
  const g = await mount(G);
  assert.ok(!draftText(g).includes('Ruf mich'), 'a stored dictation is not kept for a second send');
  g.disconnectedCallback();
  // Not stored: kept in the draft, with the note saying so.
  n = posts.length;
  job = dictate('Bis später.');
  await until(() => posts.length === n + 1, 'the dictation went on the wire');
  expire();
  await job;
  const h = await mount(G);
  assert.equal(draftText(h), 'Bis später.', 'the dictation is kept for a retry');
  assert.ok(h.shadowRoot.innerHTML.includes('No answer from the server'), 'and the composer says why');
});

await ok("a kept turn is matched to what was kept, not to the user's own words", async () => {
  const el = await mount(B);
  el.fill('Please proofread this draft');
  el._keepUnsent(B, 'proofread this draft', false);
  assert.equal(draftText(el), 'Please proofread this draft proofread this draft',
    "words of the user's that contain the turn do not swallow it");
  el._keepUnsent(B, 'proofread this draft', false);
  assert.equal(draftText(el), 'Please proofread this draft proofread this draft', 'kept once');
});

await ok('a read that never answers is given up, so the poll goes on', async () => {
  const el = await mount(D);
  hangReads = true;
  const read = el._load();
  await tick();
  expire();
  assert.equal(await read, false, 'the read ends, keeping the last state');
  hangReads = false;
  assert.ok(el.thread, 'the thread shown is kept');
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the send deadline test needs it")
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
            print("FAIL: send deadline test")
            return 1
    print("PASS: send deadline test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
