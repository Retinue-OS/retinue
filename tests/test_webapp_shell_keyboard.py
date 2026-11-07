#!/usr/bin/env python3
"""Behavioural tests for shell actions in the dashboard (webapp/components/shell-keyboard.js).

Ara's replies may carry text for the device the Android shell types on as a
Bluetooth keyboard (scripts/shell-type.py). These checks pin down the policy:
nothing runs outside the shell; a typing action runs by itself only live,
young and once; a secret never runs by itself and its text never exists on
the page; the shell members are called with the contract's arguments; and the
outcome wording.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.

    python3 tests/test_webapp_shell_keyboard.py
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

const target = new EventTarget();
globalThis.window = globalThis;
globalThis.addEventListener = target.addEventListener.bind(target);
globalThis.removeEventListener = target.removeEventListener.bind(target);
globalThis.dispatchEvent = target.dispatchEvent.bind(target);
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};

const shell = await import('./shell.js');
const kbd = await import('./shell-keyboard.js');

let passed = 0;
async function ok(name, fn) { await fn(); passed++; console.log('  ok  ' + name); }

function installShell(status = { available: true, enabled: true, connected: true, host: { name: 'Quest 3', address: 'AA' }, layout: 'de-CH' }) {
  const calls = [];
  window.RetinueShell = {
    platform: 'android',
    keyboardStatus: () => JSON.stringify(status),
    keyboardType: (text, opts) => { calls.push(['type', text, JSON.parse(opts)]); },
    keyboardKey: (name, opts) => { calls.push(['key', name, JSON.parse(opts)]); },
    keyboardSecret: (opts) => { calls.push(['secret', JSON.parse(opts)]); },
    keyboardOpen: () => { calls.push(['open']); },
  };
  return calls;
}

const now = Date.parse('2026-11-06T15:00:00Z');
const fresh = { ts: '2026-11-06T14:59:30Z' };
const stale = { ts: '2026-11-06T14:50:00Z' };
const typeA = { kind: 'keyboard-type', id: 'a'.repeat(32), text: 'Hallo Quest', enter: false, auto: true };
const keyB = { kind: 'keyboard-key', id: 'b'.repeat(32), key: 'enter' };
const keyTab = { kind: 'keyboard-key', id: '9'.repeat(32), key: 'tab', auto: true };
const secretC = { kind: 'keyboard-secret', id: 'c'.repeat(32), label: 'Meta account', enter: true };

await ok('outside the shell: no keyboard, nothing runs', () => {
  delete window.RetinueShell;
  assert.equal(shell.hasKeyboard(), false);
  assert.equal(shell.keyboardStatus(), null);
  assert.equal(kbd.shouldAutoRun(typeA, fresh, { live: true, now }), false);
  assert.equal(kbd.run(typeA), false);
});

await ok('actionsOf keeps only well-formed actions', () => {
  const m = { shell_actions: [typeA, { kind: 'x', id: 'z' }, null, { kind: 'keyboard-key' }, keyB] };
  assert.deepEqual(kbd.actionsOf(m), [typeA, keyB]);
  assert.deepEqual(kbd.actionsOf({}), []);
});

await ok('a typing action runs by itself only live, young and once', () => {
  store.clear();
  installShell();
  assert.equal(kbd.shouldAutoRun(typeA, fresh, { live: false, now }), false, 'history load');
  assert.equal(kbd.shouldAutoRun(typeA, stale, { live: true, now }), false, 'too old');
  assert.equal(kbd.shouldAutoRun(typeA, { ts: 'garbage' }, { live: true, now }), false);
  assert.equal(kbd.shouldAutoRun(typeA, fresh, { live: true, now }), true);
  assert.equal(kbd.shouldAutoRun(keyTab, fresh, { live: true, now }), true);
  assert.equal(kbd.claim(typeA.id), true);
  assert.equal(kbd.claim(typeA.id), false, 'a second tab must lose');
  assert.equal(kbd.shouldAutoRun(typeA, fresh, { live: true, now }), false, 'done before');
});

await ok('a secret never runs by itself', () => {
  store.clear();
  installShell();
  assert.equal(kbd.shouldAutoRun(secretC, fresh, { live: true, now }), false);
  assert.equal(kbd.shouldAutoRun({ ...secretC, auto: true }, fresh, { live: true, now }), false);
});

await ok('only what the gateway marked auto runs by itself, and never what presses Enter', () => {
  store.clear();
  installShell();
  const { auto: _a, ...unmarked } = typeA;
  assert.equal(kbd.shouldAutoRun(unmarked, fresh, { live: true, now }), false, 'no auto mark');
  assert.equal(kbd.shouldAutoRun({ ...typeA, auto: false }, fresh, { live: true, now }), false);
  // Even marked, the page checks again.
  assert.equal(kbd.shouldAutoRun({ ...typeA, enter: true }, fresh, { live: true, now }), false, 'Enter');
  assert.equal(kbd.shouldAutoRun({ ...typeA, text: 'ls\nrm -rf ~' }, fresh, { live: true, now }), false, 'line break');
  assert.equal(kbd.shouldAutoRun({ ...keyB, auto: true }, fresh, { live: true, now }), false, 'the Enter key');
  assert.equal(kbd.pressesEnter({ ...typeA, text: 'a\rb' }), true);
  assert.equal(kbd.pressesEnter(typeA), false);
  assert.equal(kbd.pressesEnter(keyTab), false);
});

await ok('run() calls the shell with the contract arguments; a secret carries no text', () => {
  const calls = installShell();
  assert.equal(kbd.run(typeA), true);
  assert.equal(kbd.run(keyB), true);
  assert.equal(kbd.run(secretC), true);
  assert.deepEqual(calls, [
    ['type', 'Hallo Quest', { id: typeA.id, enter: false }],
    ['key', 'enter', { id: keyB.id }],
    ['secret', { id: secretC.id, label: 'Meta account', enter: true }],
  ]);
  assert.equal(shell.keyboardOpen(), true);
});

await ok('a throwing shell counts as no call', () => {
  window.RetinueShell = { keyboardStatus: () => '{}', keyboardType: () => { throw new Error('x'); } };
  assert.equal(kbd.run(typeA), false);
});

await ok('the done list is bounded', () => {
  store.clear();
  for (let i = 0; i < 250; i++) kbd.claim(i.toString(16).padStart(32, '0'));
  assert.equal(JSON.parse(store.get('retinue.keyboard.done')).length, 200);
  assert.equal(kbd.isDone((249).toString(16).padStart(32, '0')), true);
  assert.equal(kbd.isDone((0).toString(16).padStart(32, '0')), false);
});

await ok('keyboard events reach subscribers; others do not', () => {
  const seen = [];
  const off = shell.onKeyboard((d) => seen.push(d.state));
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'keyboard', state: 'typed', id: typeA.id, count: 11 } }));
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'health-sync', state: 'done' } }));
  off();
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'keyboard', state: 'error' } }));
  assert.deepEqual(seen, ['typed']);
});

await ok('wording', () => {
  installShell();
  assert.equal(kbd.hostName(), 'Quest 3');
  assert.equal(kbd.describeAction(typeA), 'Type 11 characters');
  assert.equal(kbd.describeAction({ ...typeA, text: 'x', enter: true }), 'Type 1 character + Enter');
  assert.equal(kbd.describeAction(keyB), 'Press Enter');
  assert.equal(kbd.describeAction(secretC), 'Password from manager: Meta account');
  assert.deepEqual(kbd.describeOutcome({ state: 'typed' }, 'Quest 3'), ['Typed on Quest 3 ✓', false]);
  assert.deepEqual(kbd.describeOutcome({ state: 'error', error: 'not-connected' }), ['Not typed: no device connected', true]);
  assert.deepEqual(kbd.describeOutcome({ state: 'secret-cancelled' }), ['Password not typed: cancelled', false]);
  assert.deepEqual(kbd.describeOutcome(null), ['', false]);
});

await ok('a sensitive text is masked, reported typed, and gone once purged', async () => {
  const calls = installShell();
  const sens = { kind: 'keyboard-type', id: 'e'.repeat(32), text: 'hunter2', enter: true, sensitive: true };
  assert.equal(kbd.isSensitive(sens), true);
  assert.equal(kbd.describeAction(sens), 'Type •••••• + Enter');
  assert.ok(!kbd.describeAction(sens).includes('hunter2'));
  // It still types the real text (the phone needs it).
  assert.equal(kbd.run(sens), true);
  assert.deepEqual(calls.pop(), ['type', 'hunter2', { id: sens.id, enter: true }]);
  // Purged: nothing left to type or auto-run.
  const purged = { ...sens, text: null, purged: true };
  assert.equal(kbd.isPurged(purged), true);
  assert.equal(kbd.run(purged), false);
  assert.equal(kbd.shouldAutoRun(purged, fresh, { live: true, now }), false);
  assert.equal(kbd.describeAction({ ...typeA, text: null, purged: true }), 'Text deleted');
  assert.deepEqual(kbd.describeOutcome({ state: 'deleted' }), ['Text deleted', false]);
});

await ok('every final outcome is reported to the gateway; the stored one comes back', async () => {
  const posted = [];
  const fakeFetch = async (url, opts) => { posted.push([url, opts.method, JSON.parse(opts.body)]); return { ok: true }; };
  const tid = 'f'.repeat(32);
  assert.equal(await kbd.reportOutcome(tid, typeA, { state: 'typed', count: 11 }, fakeFetch), true);
  assert.equal(await kbd.reportOutcome(tid, keyB, { state: 'error', error: 'not-connected' }, fakeFetch), true);
  assert.equal(await kbd.reportOutcome(tid, secretC, { state: 'secret-cancelled' }, fakeFetch), true);
  assert.equal(await kbd.reportOutcome(tid, typeA, { state: 'status' }, fakeFetch), false, 'not final');
  assert.equal(await kbd.reportOutcome(tid, typeA, { state: 'pending' }, fakeFetch), false);
  assert.deepEqual(posted, [
    [`/conversations/${tid}/shell-actions/${typeA.id}/outcome`, 'POST', { state: 'typed' }],
    [`/conversations/${tid}/shell-actions/${keyB.id}/outcome`, 'POST', { state: 'error', error: 'not-connected' }],
    [`/conversations/${tid}/shell-actions/${secretC.id}/outcome`, 'POST', { state: 'secret-cancelled' }],
  ]);
  const failing = async () => { throw new Error('offline'); };
  assert.equal(await kbd.reportOutcome(tid, typeA, { state: 'typed' }, failing), false);
  assert.deepEqual(kbd.storedOutcome({ ...typeA, outcome: { state: 'typed', at: 'x' } }), { state: 'typed', error: undefined });
  assert.equal(kbd.storedOutcome({ ...typeA, outcome: { state: 'bogus' } }), null);
  assert.equal(kbd.storedOutcome(typeA), null);
});

console.log(`${passed} shell-keyboard checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the shell-keyboard behaviour test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("shell.js", "shell-keyboard.js"):
            shutil.copy(os.path.join(COMPONENTS, name), os.path.join(tmp, name))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stdout.write(proc.stderr)
        print("FAIL: shell-keyboard behaviour")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
