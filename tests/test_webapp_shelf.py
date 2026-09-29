#!/usr/bin/env python3
"""Behavioural tests for the shelf's rules (webapp/components/shelf-store.js).

The shelf (issue #282) parks threads, chats and projects and shows what each
one is waiting on. Its rules are a pure reducer, so this test drives it under
Node without a browser and pins the issue's behavioural invariants: one item
per key, explicit over automatic, automatic items living only for their turn,
Close suppressing re-adding for that turn, capacity evicting quiet items only,
projects never carrying an Ara state, archive and mute. A second part pins the
persistence layer: a change that changes nothing writes nothing (so other tabs
are not woken), and the size setting evicts on the spot.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "..", "webapp", "components", "shelf-store.js")

HARNESS = r"""
import assert from 'node:assert/strict';

// A localStorage that counts writes, and a sessionStorage.
const mkStorage = () => {
  const m = new Map();
  return {
    writes: 0,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem(k, v) { this.writes += 1; m.set(k, String(v)); },
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
  };
};
globalThis.localStorage = mkStorage();
globalThis.sessionStorage = mkStorage();
const winListeners = new Map();
globalThis.window = globalThis;
globalThis.addEventListener = (t, f) => { if (!winListeners.has(t)) winListeners.set(t, []); winListeners.get(t).push(f); };

const S = await import('./shelf-store.js');
const { reduce, emptyState, isQuiet } = S;

let passed = 0;
const ok = (name, fn) => { fn(); passed += 1; console.log(`ok - ${name}`); };

const T0 = '2026-09-29T10:00:00Z';
const T1 = '2026-09-29T10:01:00Z';
const T2 = '2026-09-29T10:02:00Z';
const run = (actions, size = 5) => {
  let s = emptyState();
  let now = 1000;
  for (const a of actions) { now += 1; s = reduce(s, { now, ...a }, size); }
  return s;
};
const keys = (s) => s.items.map((i) => i.key);
const item = (s, k) => s.items.find((i) => i.key === k);
const thread = (id, extra = {}) => ({ key: `thread:${id}`, kind: 'thread', id, title: id, ...extra });
const leftPending = (id, turn = T0, extra = {}) =>
  ({ type: 'left', item: thread(id, { pending: true, turn, lastAraTs: '', ...extra }) });

ok('leaving a pending thread adds one automatic, working item', () => {
  const s = run([leftPending('a')]);
  assert.deepEqual(keys(s), ['thread:a']);
  assert.equal(item(s, 'thread:a').origin, 'automatic');
  assert.equal(item(s, 'thread:a').araState, 'working');
});

ok('leaving a thread that is not pending adds nothing', () => {
  const s = run([{ type: 'left', item: thread('a', { pending: false }) }]);
  assert.deepEqual(keys(s), []);
});

ok('one item per key, explicit wins over automatic and is never downgraded', () => {
  const s = run([
    leftPending('a'),
    { type: 'minimize', item: thread('a', { pending: true, turn: T0 }) },
    leftPending('a'),
  ]);
  assert.deepEqual(keys(s), ['thread:a']);
  assert.equal(item(s, 'thread:a').origin, 'explicit');
});

ok('the answer turns a working item ready; seeing it removes an automatic item', () => {
  let s = run([leftPending('a')]);
  s = reduce(s, { type: 'observe', key: 'thread:a', obs: { pending: false, turn: '', lastAraTs: T1 } });
  assert.equal(item(s, 'thread:a').araState, 'ready');
  s = reduce(s, { type: 'seen', key: 'thread:a', ts: T1 });
  assert.deepEqual(keys(s), [], 'the automatic item has done its job');
});

ok('seeing an older message does not consume the ready state', () => {
  let s = run([leftPending('a', T0, { lastAraTs: T0 })]);
  s = reduce(s, { type: 'observe', key: 'thread:a', obs: { pending: false, lastAraTs: T2 } });
  s = reduce(s, { type: 'seen', key: 'thread:a', ts: T0 });
  assert.equal(item(s, 'thread:a').araState, 'ready');
});

ok('an explicit item stays after its answer is seen, now quiet', () => {
  let s = run([{ type: 'minimize', item: thread('a', { pending: true, turn: T0 }) }]);
  s = reduce(s, { type: 'observe', key: 'thread:a', obs: { pending: false, lastAraTs: T1 } });
  s = reduce(s, { type: 'seen', key: 'thread:a', ts: T1 });
  assert.deepEqual(keys(s), ['thread:a']);
  assert.ok(isQuiet(item(s, 'thread:a')));
});

ok('a new answer on an explicit item marks it ready again', () => {
  let s = run([{ type: 'minimize', item: thread('a', { lastAraTs: T0 }) }]);
  assert.equal(item(s, 'thread:a').araState, 'idle', 'what was on screen counts as seen');
  s = reduce(s, { type: 'observe', key: 'thread:a', obs: { lastAraTs: T1 } });
  assert.equal(item(s, 'thread:a').araState, 'ready');
});

ok('closing a working item suppresses re-adding for that turn only', () => {
  let s = run([leftPending('a', T0)]);
  s = reduce(s, { type: 'observe', key: 'thread:a', obs: { pending: true, turn: T0 } });
  s = reduce(s, { type: 'close', key: 'thread:a' });
  s = reduce(s, leftPending('a', T0));
  assert.deepEqual(keys(s), [], 'same turn: not waiting any more');
  s = reduce(s, leftPending('a', T1));
  assert.deepEqual(keys(s), ['thread:a'], 'a new turn is unaffected');
});

ok('the suppression lapses with its turn', () => {
  let s = run([leftPending('a', T0), { type: 'close', key: 'thread:a' }]);
  s = reduce(s, { type: 'observe', key: 'thread:a', obs: { pending: false, turn: '' } });
  assert.deepEqual(s.suppressed, {});
});

ok('explicit minimize works whatever was suppressed', () => {
  let s = run([leftPending('a', T0), { type: 'close', key: 'thread:a' }]);
  s = reduce(s, { type: 'minimize', item: thread('a', { pending: true, turn: T0 }) });
  assert.deepEqual(keys(s), ['thread:a']);
});

ok('capacity evicts the oldest quiet item', () => {
  const s = run([
    { type: 'minimize', item: thread('a') },
    { type: 'minimize', item: thread('b') },
    { type: 'minimize', item: thread('c') },
  ], 2);
  assert.deepEqual(keys(s), ['thread:b', 'thread:c']);
});

ok('items with a marker are never evicted; the shelf holds more than its size', () => {
  const s = run([leftPending('a'), leftPending('b'), leftPending('c')], 2);
  assert.deepEqual(keys(s), ['thread:a', 'thread:b', 'thread:c']);
});

ok('an over-full shelf shrinks back as items go quiet', () => {
  let s = run([
    { type: 'minimize', item: thread('a', { pending: true, turn: T0 }) },
    { type: 'minimize', item: thread('b', { pending: true, turn: T0 }) },
  ], 1);
  assert.equal(s.items.length, 2);
  s = reduce(s, { type: 'observe', key: 'thread:a', obs: { pending: false, lastAraTs: T1 } }, 1);
  s = reduce(s, { type: 'seen', key: 'thread:a', ts: T1 }, 1);
  assert.deepEqual(keys(s), ['thread:b'], 'a went quiet and made room');
});

ok('unread human messages keep a chat from eviction', () => {
  let s = run([{ type: 'minimize', item: { key: 'chat:x', kind: 'chat', unread: 3 } },
               { type: 'minimize', item: thread('b') }], 1);
  assert.deepEqual(keys(s), ['chat:x']);
  assert.equal(item(s, 'chat:x').araState, 'idle');
});

ok('lowering the size trims quiet items, oldest first', () => {
  let s = run([
    { type: 'minimize', item: thread('a') },
    { type: 'minimize', item: thread('b') },
    { type: 'touch', key: 'thread:a' },
  ]);
  s = reduce(s, { type: 'resize' }, 1);
  assert.deepEqual(keys(s), ['thread:a'], 'b was used longest ago');
});

ok('projects never carry an Ara state', () => {
  const s = run([{ type: 'minimize', item: { key: 'project:p', kind: 'project', pending: true, lastAraTs: T1 } }]);
  assert.equal(item(s, 'project:p').araState, 'idle');
});

ok('archiving removes an automatic item and keeps it from coming back for that turn', () => {
  let s = run([leftPending('a', T0), { type: 'archived', key: 'thread:a', archived: true, turn: T0 }]);
  assert.deepEqual(keys(s), []);
  s = reduce(s, leftPending('a', T0));
  assert.deepEqual(keys(s), [], 'the host leaving the archived thread does not re-add it');
});

ok('archiving keeps an explicit item', () => {
  const s = run([{ type: 'minimize', item: thread('a') }, { type: 'archived', key: 'thread:a', archived: true }]);
  assert.deepEqual(keys(s), ['thread:a']);
});

ok('mute removes whatever the origin, and a muted page is never auto-added', () => {
  let s = run([{ type: 'minimize', item: thread('a') }, { type: 'muted', key: 'thread:a' }]);
  assert.deepEqual(keys(s), []);
  s = reduce(s, leftPending('b', T0, { muted: true }));
  assert.deepEqual(keys(s), []);
  s = run([{ type: 'minimize', item: thread('c') }, { type: 'observe', key: 'thread:c', obs: { muted: true } }]);
  assert.deepEqual(keys(s), [], 'a mute learned from a read counts too');
});

ok('close all quiet ones leaves the marked items', () => {
  const s = run([{ type: 'minimize', item: thread('a') }, leftPending('b'), { type: 'closeQuiet' }]);
  assert.deepEqual(keys(s), ['thread:b']);
});

ok('threadFacts reads pending, the turn and the newest Ara message', () => {
  const f = S.threadFacts({ pending: true, pending_since: T2, muted: false, messages: [
    { role: 'user', ts: T0 }, { role: 'ara', ts: T1 }, { role: 'user', ts: T2 }] });
  assert.deepEqual(f, { pending: true, turn: T2, lastAraTs: T1, muted: false });
});

// ── Persistence ──────────────────────────────────────────────────────────────
ok('an observation that changes nothing writes nothing', () => {
  S.shelf.minimize(thread('p'));
  const w = localStorage.writes;
  S.shelf.observe('thread:p', { pending: false });
  S.shelf.observe('thread:nope', { pending: false });
  assert.equal(localStorage.writes, w);
});

ok('subscribers hear changes from this tab and from others', () => {
  let heard = 0;
  const off = S.subscribe(() => { heard += 1; });
  S.shelf.minimize(thread('q'));
  assert.equal(heard, 1);
  for (const f of winListeners.get('storage') || []) f({ key: S.STATE_KEY });
  assert.equal(heard, 2, 'a write in another tab arrives as a storage event');
  off();
});

ok('the size setting is clamped and evicts at once', () => {
  S.setShelfSize(1);
  assert.equal(S.shelfSize(), 1);
  assert.equal(S.shelf.items().length, 1);
  assert.equal(S.setShelfSize(0), S.DEFAULT_SIZE);
  assert.equal(S.setShelfSize(999), S.MAX_SIZE);
});

ok('a restore is handed over once, to the key it was meant for', () => {
  S.setRestore('thread:r', { anchor: T0, offset: 12 });
  assert.equal(S.takeRestore('thread:other'), null);
  assert.deepEqual(S.takeRestore('thread:r'), { anchor: T0, offset: 12 });
  assert.equal(S.takeRestore('thread:r'), null);
});

console.log(`${passed} checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the shelf behaviour test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(STORE, os.path.join(tmp, "shelf-store.js"))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: shelf behaviour test")
            return 1
    print("PASS: shelf behaviour test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
