#!/usr/bin/env python3
"""A dictation the user sent reaches the thread it was recorded in (webapp/sw.js, "Sent dictations").

➤ and a take of the continuous loop ending on a pause are messages to their
thread, as if typed there. The page that recorded one sends it once the
transcript is in; a page gone by then cannot, so the service worker does. The
test runs the worker under Node against an in-memory IndexedDB, scripted
clients and a fake network, and checks: a live recording page still sends
itself (the worker does not); a page gone before the transcript has it sent
into the recorded thread — and only that thread, only the dashboard's own
messages endpoint; a page showing the thread meanwhile is told it went; a
refused send is never retried and goes to the draft; a review dictation (✓,
a hand-off on leaving) and an empty transcript are never sent.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SW_JS = os.path.join(HERE, "..", "webapp", "sw.js")

HARNESS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// ── A minimal in-memory IndexedDB: one database, one keyPath store ──────────
const DB = new Map();
const later = (fn) => setTimeout(fn, 0);
function request(run) {
  const req = {};
  later(() => { req.result = run(); if (req.onsuccess) req.onsuccess(); });
  return req;
}
const indexedDB = {
  open() {
    const req = {};
    later(() => {
      req.result = {
        close() {}, createObjectStore() {},
        transaction() {
          const tx = {};
          let pending = 0;
          const track = (r) => { pending += 1; const s = r.onsuccess; return r; };
          const store = {
            get: (id) => { const r = request(() => (DB.has(id) ? structuredClone(DB.get(id)) : undefined)); return r; },
            getAll: () => request(() => [...DB.values()].map((v) => structuredClone(v))),
            put: (v) => request(() => { DB.set(v.id, structuredClone(v)); }),
            delete: (id) => request(() => { DB.delete(id); }),
          };
          tx.objectStore = () => store;
          setTimeout(() => tx.oncomplete && tx.oncomplete(), 5);
          return tx;
        },
      };
      if (req.onupgradeneeded) req.onupgradeneeded();
      if (req.onsuccess) req.onsuccess();
    });
    return req;
  },
};

// ── Clients and network ─────────────────────────────────────────────────────
const CLIENTS = new Map();   // id -> {posted: []}
const POSTS = [];            // message POSTs the worker made
let sendStatus = 200;
let transcript = 'hello thread';
let hangSends = false;       // a message POST that never answers, until its signal aborts
async function fetchMock(url, init = {}) {
  if (String(url).startsWith('/conversations/transcribe')) {
    return { ok: true, json: async () => ({ text: transcript }) };
  }
  POSTS.push({ url, body: JSON.parse(init.body) });
  if (hangSends) {
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    });
  }
  return { ok: sendStatus < 400, status: sendStatus, json: async () => ({}) };
}
const listeners = {};
const self = {
  addEventListener: (t, fn) => { (listeners[t] ||= []).push(fn); },
  clients: { get: async (id) => (CLIENTS.has(id) ? { postMessage: (m) => CLIENTS.get(id).posted.push(m) } : undefined),
             matchAll: async () => [], claim: async () => {} },
  registration: {}, skipWaiting: () => {},
};
// The worker's 30 s send deadline is made short; every other timer is as it is.
const shortTimeout = (fn, ms, ...rest) => setTimeout(fn, ms === 30000 ? 30 : ms, ...rest);
const ctx = vm.createContext({ self, indexedDB, fetch: fetchMock, Blob, AbortController, setTimeout: shortTimeout, clearTimeout,
  console, Response: class {}, caches: {}, URL, structuredClone });
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);

function message(data, sourceId, ports) {
  let done = Promise.resolve();
  const e = { data, source: sourceId ? { id: sourceId, postMessage: (m) => CLIENTS.get(sourceId)?.posted.push(m) } : null,
              ports: ports || [], waitUntil: (p) => { done = p; } };
  for (const fn of listeners.message || []) fn(e);
  return done;
}
function adopt(keys, asker) {
  return new Promise((resolve) => {
    const port = { postMessage: (m) => resolve(m.jobs) };
    message({ type: 'dictation-adopt', keys }, asker, [port]);
  });
}
const blob = new Blob(['audio'], { type: 'audio/webm' });
const SEND = '/conversations/t1/messages';
const reset = () => { DB.clear(); CLIENTS.clear(); POSTS.length = 0; sendStatus = 200; hangSends = false; transcript = 'hello thread'; };

// 1. ➤, page still there and owning the job: the worker does NOT send; the page hears send:true.
reset(); CLIENTS.set('A', { posted: [] });
await message({ type: 'dictation', id: 'j1', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob, send: SEND }, 'A');
assert.equal(POSTS.length, 0, 'live recording page sends itself');
const done1 = CLIENTS.get('A').posted.find((m) => m.type === 'dictation-done');
assert.equal(done1.text, 'hello thread'); assert.equal(done1.send, true); assert.equal(done1.sent, false);

// 2. ➤, page gone (released) before the transcript: the worker sends into the RECORDED thread.
reset(); CLIENTS.set('A', { posted: [] });
const run2 = message({ type: 'dictation', id: 'j2', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob, send: SEND }, 'A');
CLIENTS.delete('A'); await message({ type: 'dictation-release' }, 'A');
await run2;
assert.deepEqual(POSTS, [{ url: SEND, body: { message: 'hello thread' } }]);
assert.equal(DB.size, 0, 'sent with nobody to tell: the job is gone, nothing for a draft');

// 3. Page gone, but another page now shows the thread and adopted the job mid-run: sent, and that page told sent:true.
reset(); CLIENTS.set('A', { posted: [] }); CLIENTS.set('B', { posted: [] });
const run3 = message({ type: 'dictation', id: 'j3', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob, send: SEND }, 'A');
CLIENTS.delete('A');
await adopt(['conv:t1'], 'B');
await run3;
assert.equal(POSTS.length, 1);
const done3 = CLIENTS.get('B').posted.find((m) => m.type === 'dictation-done');
assert.equal(done3.sent, true, 'the page showing the thread learns it went');

// 4. Send refused: unsent 'failed', kept for the draft, not deleted, no retry.
reset(); CLIENTS.set('A', { posted: [] }); sendStatus = 500;
const run4 = message({ type: 'dictation', id: 'j4', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob, send: SEND }, 'A');
CLIENTS.delete('A'); await message({ type: 'dictation-release' }, 'A');
await run4;
assert.equal(POSTS.length, 1);
CLIENTS.set('C', { posted: [] });
const jobs4 = await adopt(['conv:t1'], 'C');
assert.equal(jobs4.length, 1); assert.equal(jobs4[0].unsent, 'failed'); assert.equal(jobs4[0].text, 'hello thread');
assert.equal(POSTS.length, 1, 'never retried');

// 5. ✓ (review) or a handed-off recording: never sent by the worker.
reset(); CLIENTS.set('A', { posted: [] });
const run5 = message({ type: 'dictation', id: 'j5', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob }, 'A');
CLIENTS.delete('A'); await message({ type: 'dictation-release' }, 'A');
await run5;
await message({ type: 'dictation', id: 'j6', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob, orphan: true }, 'A');
assert.equal(POSTS.length, 0, 'review dictations stay drafts');

// 6. Only the dashboard's own messages endpoint: a foreign target is ignored.
reset(); CLIENTS.set('A', { posted: [] });
const run6 = message({ type: 'dictation', id: 'j7', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob, send: 'https://evil.example/x' }, 'A');
CLIENTS.delete('A'); await message({ type: 'dictation-release' }, 'A');
await run6;
assert.equal(POSTS.length, 0);

// 7. No speech: nothing is sent.
reset(); CLIENTS.set('A', { posted: [] }); transcript = '';
const run7 = message({ type: 'dictation', id: 'j8', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob, send: SEND }, 'A');
CLIENTS.delete('A'); await message({ type: 'dictation-release' }, 'A');
await run7;
assert.equal(POSTS.length, 0);

// 8. A send that gets no answer within its deadline is aborted and is 'maybe' (it may have
//    reached the thread): kept for the draft, exactly one POST, never retried.
reset(); CLIENTS.set('A', { posted: [] }); hangSends = true;
const run8 = message({ type: 'dictation', id: 'j9', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob, send: SEND }, 'A');
CLIENTS.delete('A'); await message({ type: 'dictation-release' }, 'A');
await run8;
assert.equal(POSTS.length, 1, 'one POST');
CLIENTS.set('C', { posted: [] });
const jobs8 = await adopt(['conv:t1'], 'C');
assert.equal(jobs8.length, 1);
assert.equal(jobs8[0].unsent, 'maybe', 'a timeout may have gone through: not failed, not retried');
assert.equal(jobs8[0].sent, false); assert.equal(jobs8[0].text, 'hello thread');
assert.equal(POSTS.length, 1, 'never retried');

// 9. A worker stopped while sending (record 'running' in phase 'sending', not in this worker's
//    running set): adopting it does not send again; it becomes 'maybe' with its text kept.
reset(); CLIENTS.set('C', { posted: [] });
DB.set('j10', { id: 'j10', key: 'conv:t1', url: '/conversations/transcribe?thread=t1', blob: null, owner: '', origin: 'A',
                send: SEND, state: 'running', phase: 'sending', text: 'hello thread', error: '', at: Date.now() });
const jobs9 = await adopt(['conv:t1'], 'C');
assert.equal(jobs9.length, 1);
assert.equal(jobs9[0].state, 'done'); assert.equal(jobs9[0].unsent, 'maybe'); assert.equal(jobs9[0].text, 'hello thread');
assert.equal(POSTS.length, 0, 'a record stranded mid-send is never sent again');
await new Promise((r) => setTimeout(r, 50));
assert.equal(POSTS.length, 0, 'nor restarted afterwards');
console.log('ok');
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("skip: node not on PATH")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "harness.mjs")
        with open(path, "w", encoding="utf-8") as f:
            f.write(HARNESS)
        res = subprocess.run([node, path, os.path.abspath(SW_JS)], capture_output=True, text=True, timeout=60)
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    return res.returncode


if __name__ == "__main__":
    sys.exit(main())
