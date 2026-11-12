#!/usr/bin/env python3
"""Behavioural tests for shell actions in the dashboard (webapp/components/shell-keyboard.js).

Ara's replies may carry text for the device the Android shell types on as a
Bluetooth keyboard (scripts/shell-type.py). These checks pin down the policy:
nothing runs outside the shell; only plain one-line text runs by itself, and
only live, young and once; a secret never runs by itself and its text never
exists on the page; actions run one at a time, every outcome is reported, and
Stop drops what waits, as does a confirmation on the phone declined or not
answered, or a password dialog cancelled there; a sensitive text is gone
from the page after two
minutes; the shell members are called with the contract's arguments; and the
outcome wording.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.

    python3 tests/test_webapp_shell_keyboard.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(HERE, "..", "webapp", "components")

# Every character Python's str.strip() takes off an edge: the set
# shell_actions.runs_by_itself judges with, handed to the page's twin below.
PY_WHITESPACE = "".join(chr(c) for c in range(sys.maxunicode + 1)
                        if ("a" + chr(c)).strip() == "a")

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
// What the module reports to the gateway.
const posted = [];
globalThis.fetch = async (url, opts) => { posted.push([url, JSON.parse(opts.body)]); return { ok: true }; };
const tick = () => new Promise((r) => setTimeout(r, 0));

const shell = await import('./shell.js');
const kbd = await import('./shell-keyboard.js');

let passed = 0;
async function ok(name, fn) { kbd._reset(); posted.length = 0; await fn(); passed++; console.log('  ok  ' + name); }

function installShell(status = { available: true, enabled: true, connected: true, host: { name: 'Quest 3' }, layout: 'de-CH' }) {
  const calls = [];
  window.RetinueShell = {
    platform: 'android',
    keyboardStatus: () => JSON.stringify(status),
    keyboardType: (text, opts) => { calls.push(['type', text, JSON.parse(opts)]); },
    keyboardKey: (name, opts) => { calls.push(['key', name, JSON.parse(opts)]); },
    keyboardSecret: (opts) => { calls.push(['secret', JSON.parse(opts)]); },
    keyboardOpen: () => { calls.push(['open']); },
    keyboardCancel: () => { calls.push(['cancel']); },
  };
  return calls;
}
function event(state, id, extra = {}) {
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'keyboard', state, id, ...extra } }));
}

const now = Date.parse('2026-11-06T15:00:00Z');
const fresh = { ts: '2026-11-06T14:59:30Z' };
const stale = { ts: '2026-11-06T14:50:00Z' };
const typeA = { kind: 'keyboard-type', id: 'a'.repeat(32), text: 'Hallo Quest', enter: false, auto: true };
const keyB = { kind: 'keyboard-key', id: 'b'.repeat(32), key: 'enter' };
const keyTab = { kind: 'keyboard-key', id: '9'.repeat(32), key: 'tab', auto: true };
const secretC = { kind: 'keyboard-secret', id: 'c'.repeat(32), label: 'Meta account', enter: true };
const tid = 'f'.repeat(32);

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

await ok('plain text runs by itself only live, young and once', () => {
  store.clear();
  installShell();
  assert.equal(kbd.shouldAutoRun(typeA, fresh, { live: false, now }), false, 'history load');
  assert.equal(kbd.shouldAutoRun(typeA, stale, { live: true, now }), false, 'too old');
  assert.equal(kbd.shouldAutoRun(typeA, { ts: 'garbage' }, { live: true, now }), false);
  assert.equal(kbd.shouldAutoRun(typeA, fresh, { live: true, now }), true);
  assert.equal(kbd.claim(typeA.id), true);
  assert.equal(kbd.claim(typeA.id), false, 'claimed once');
  assert.equal(kbd.shouldAutoRun(typeA, fresh, { live: true, now }), false, 'done before');
});

await ok('a secret never runs by itself', () => {
  store.clear();
  installShell();
  assert.equal(kbd.shouldAutoRun(secretC, fresh, { live: true, now }), false);
  assert.equal(kbd.shouldAutoRun({ ...secretC, auto: true }, fresh, { live: true, now }), false);
});

await ok("only a message's first action runs by itself, even if a later one is marked", () => {
  store.clear();
  installShell();
  // A later text typed at once would land ahead of what waits for a tap:
  // 'cd /tmp' + Enter confirmed afterwards would run 'rm -rf buildcd /tmp'.
  const first = { kind: 'keyboard-type', id: 'e'.repeat(32), text: 'cd /tmp', enter: true, auto: false };
  for (const before of [first, keyTab]) {
    const m = { ...fresh, shell_actions: [before, typeA] };
    assert.equal(kbd.shouldAutoRun(typeA, m, { live: true, now }), false, before.kind);
  }
  assert.equal(kbd.shouldAutoRun(typeA, { ...fresh, shell_actions: [typeA, keyTab] },
    { live: true, now }), true, 'the first one does');
});

await ok('only plain one-line text the gateway marked auto runs by itself', () => {
  store.clear();
  installShell();
  const { auto: _a, ...unmarked } = typeA;
  assert.equal(kbd.shouldAutoRun(unmarked, fresh, { live: true, now }), false, 'no auto mark');
  assert.equal(kbd.shouldAutoRun({ ...typeA, auto: false }, fresh, { live: true, now }), false);
  // Even marked, the page checks again: whatever can submit or act waits for a tap.
  for (const [what, a] of [
    ['Enter', { ...typeA, enter: true }],
    ['a line break', { ...typeA, text: 'ls\nrm -rf ~' }],
    ['a carriage return', { ...typeA, text: 'a\rb' }],
    ['a tab (to a form button)', { ...typeA, text: 'me@x.org\t\t ' }],
    // With no whitespace at an edge, only the tab itself makes it wait.
    ['an inner tab', { ...typeA, text: 'a\tb' }],
    ['a sensitive text', { ...typeA, sensitive: true }],
    ['the Enter key', { ...keyB, auto: true }],
    ['the Tab key', keyTab],
    ['the Space key', { ...keyTab, key: 'space' }],
  ]) {
    assert.equal(kbd.needsTap(a), true, what);
    assert.equal(kbd.shouldAutoRun(a, fresh, { live: true, now }), false, what);
  }
  assert.equal(kbd.needsTap(typeA), false);
  // Nor only whitespace, nor whitespace at an edge (a space presses a focused
  // button) — as shell_actions.runs_by_itself, tests/test_shell_type.py.
  for (const text of [' ', '  ', ' a', 'a ', ' x']) {
    assert.equal(kbd.needsTap({ ...typeA, text }), true, JSON.stringify(text));
    assert.equal(kbd.shouldAutoRun({ ...typeA, text }, fresh, { live: true, now }), false, JSON.stringify(text));
  }
  assert.equal(kbd.needsTap({ ...typeA, text: 'a b' }), false);
  // Every character Python's strip() takes, at either edge, waits for a tap:
  // the list comes from Python itself (PY_WHITESPACE, put in by main()), so
  // the page and shell_actions.runs_by_itself cannot drift apart. trim()
  // alone misses U+0085 and U+001C-U+001F.
  const pyWhitespace = __PY_WHITESPACE__;
  assert.ok(pyWhitespace.length >= 29, 'the list made it into the harness');
  for (const c of pyWhitespace) {
    for (const text of ['a' + c, c + 'a']) {
      assert.equal(kbd.needsTap({ ...typeA, text }), true, JSON.stringify(text));
    }
  }
  for (const c of ['\u3000', '\u2028', '\u000b', '\u0085', '\u001c', '\u001d', '\u001e', '\u001f']) {
    assert.ok(pyWhitespace.includes(c), JSON.stringify(c));
    assert.equal(kbd.needsTap({ ...typeA, text: 'a' + c }), true, JSON.stringify(c));
    assert.equal(kbd.needsTap({ ...typeA, text: c + 'a' }), true, JSON.stringify(c));
  }
  // trim()'s U+FEFF, not whitespace to Python, still waits: the page may be
  // the stricter of the two, never the looser.
  assert.equal(kbd.needsTap({ ...typeA, text: 'a\ufeff' }), true);
  // Inside the text, whitespace other than a tab or a line break is plain.
  assert.equal(kbd.needsTap({ ...typeA, text: 'a\u3000b' }), false);
});

await ok('the phone confirms Enter, a line break, a tab and any key; the row says so', () => {
  const calls = installShell();
  for (const [what, a, asks] of [
    ['plain text', typeA, false],
    ['a sensitive text without Enter', { ...typeA, sensitive: true }, false],
    ['Enter', { ...typeA, enter: true }, true],
    ['a line break', { ...typeA, text: 'a\nb' }, true],
    ['a tab', { ...typeA, text: 'a\tb' }, true],
    ['the Enter key', keyB, true],
    ['the Space key', { ...keyTab, key: 'space' }, true],
    ['a password-manager field', secretC, false],
  ]) assert.equal(kbd.phoneConfirms(a), asks, what);
  const states = [];
  const onState = (out) => states.push(out);
  kbd.run({ ...typeA, text: 'ls', enter: true }, { threadId: tid, onState });
  assert.deepEqual(states.at(-1), { state: 'pending', confirm: true });
  assert.deepEqual(kbd.describeOutcome(states.at(-1), 'Quest 3'),
    ['Waiting for confirmation on the phone…', ''], 'an Enter text: the phone asks first');
  kbd._reset();
  kbd.run(keyB, { threadId: tid, onState });
  assert.deepEqual(kbd.describeOutcome(states.at(-1), 'Quest 3'),
    ['Waiting for confirmation on the phone…', ''], 'a key: the phone asks first');
  kbd._reset();
  kbd.run(typeA, { threadId: tid, onState });
  assert.deepEqual(states.at(-1), { state: 'pending' });
  assert.deepEqual(kbd.describeOutcome(states.at(-1), 'Quest 3'), ['Typing on Quest 3…', ''],
    'plain text types at once');
  assert.equal(calls.length, 3);
});

await ok('liveArrivals: the first read and the first after a hidden page only learn', () => {
  const m1 = { role: 'user', ts: '1' };
  const m2 = { role: 'assistant', ts: '2' };
  const m3 = { role: 'assistant', ts: '3' };
  let r = kbd.liveArrivals([m1, m2], null);
  assert.deepEqual(r.live, [], 'first read');
  r = kbd.liveArrivals([m1, m2, m3], r.seen);
  assert.deepEqual(r.live, [m3], 'arrived while open');
  r = kbd.liveArrivals([m1, m2, m3], r.seen);
  assert.deepEqual(r.live, [], 'nothing new');
  // The view drops its set when the page is hidden: what came meanwhile is learned, not run.
  r = kbd.liveArrivals([m1, m2, m3, { role: 'assistant', ts: '4' }], null);
  assert.deepEqual(r.live, []);
});

await ok('actions run one after another, each on the previous one\'s final event', async () => {
  const calls = installShell();
  const states = [];
  const onState = (out) => states.push(out.state);
  assert.equal(kbd.run(typeA, { threadId: tid, onState }), true);
  assert.equal(kbd.run(keyB, { threadId: tid, onState }), true);
  assert.equal(kbd.run(secretC, { threadId: tid, onState }), true);
  assert.equal(kbd.run(keyB, { threadId: tid, onState }), true, 'already queued: not twice');
  assert.deepEqual(calls, [['type', 'Hallo Quest', { id: typeA.id, enter: false }]], 'one at a time');
  assert.deepEqual(states, ['pending', 'queued', 'queued']);
  event('typed', typeA.id, { count: 11 });
  assert.deepEqual(calls.at(-1), ['key', 'enter', { id: keyB.id }]);
  // An error of this one action only: the next starts (one that stops the
  // rest: 'a declined or unanswered confirmation drops the rest too').
  event('error', keyB.id, { error: 'failed' });
  assert.deepEqual(calls.at(-1), ['secret', { id: secretC.id, label: 'Meta account', enter: true }], 'a secret carries no text');
  event('secret-cancelled', secretC.id);
  assert.equal(kbd.isRunning(), false);
  await tick();
  // Every outcome reached the gateway — from the module, whatever the view does.
  assert.deepEqual(posted, [
    [`/conversations/${tid}/shell-actions/${typeA.id}/outcome`, { state: 'typed' }],
    [`/conversations/${tid}/shell-actions/${keyB.id}/outcome`, { state: 'error', error: 'failed' }],
    [`/conversations/${tid}/shell-actions/${secretC.id}/outcome`, { state: 'secret-cancelled' }],
  ]);
  assert.equal(shell.keyboardOpen(), true);
});

await ok('Stop ends the one typing and drops the rest, reported as cancelled', async () => {
  const calls = installShell();
  const heard = [];
  const off = kbd.onOutcomeReported((r) => heard.push([r.id, r.state, r.ok]));
  kbd.run(typeA, { threadId: tid });
  kbd.run(keyTab, { threadId: tid });
  assert.equal(kbd.cancel(), true);
  assert.deepEqual(calls.at(-1), ['cancel']);
  event('error', typeA.id, { error: 'cancelled' });
  await tick();
  off();
  assert.equal(kbd.isRunning(), false);
  assert.deepEqual(posted.map((x) => x[1]), [{ state: 'error', error: 'cancelled' }, { state: 'error', error: 'cancelled' }]);
  assert.deepEqual(heard.map((h) => h[0]).sort(), [keyTab.id, typeA.id].sort());
});

await ok('a declined or unanswered confirmation drops the rest too', async () => {
  // Ara's Tab, then a text meant for the field the Tab moves to: the phone
  // asks for the key, and the user declines or lets it run out. The text
  // must not type into the field that still has focus.
  for (const error of ['cancelled', 'not-enabled', 'not-in-front', 'not-connected']) {
    kbd._reset();
    posted.length = 0;
    const calls = installShell();
    const rows = new Map();
    const onState = (id) => (out) => rows.set(id, out);
    const pin = { ...typeA, id: 'd'.repeat(32), text: 'hunter2', sensitive: true };
    kbd.run(keyTab, { threadId: tid, onState: onState(keyTab.id) });
    kbd.run(pin, { threadId: tid, onState: onState(pin.id), message: { ts: new Date().toISOString() } });
    assert.deepEqual(rows.get(pin.id), { state: 'queued' });
    event('error', keyTab.id, { error });
    assert.equal(calls.filter((c) => c[0] === 'type').length, 0, `${error}: the queued text never types`);
    assert.deepEqual(rows.get(pin.id), { state: 'error', error: 'cancelled' }, error);
    assert.equal(kbd.isRunning(), false, error);
    await tick();
    assert.deepEqual(posted.map(([url, body]) => [url.split('/')[4], body]).sort(), [
      [keyTab.id, { state: 'error', error }],
      [pin.id, { state: 'error', error: 'cancelled' }],
    ].sort(), error);
  }
  // An error that says nothing about the device or the user moves on.
  for (const error of ['failed', 'busy', 'unsupported-char', 'too-long']) {
    kbd._reset();
    const calls = installShell();
    kbd.run(keyTab, { threadId: tid });
    kbd.run(typeA, { threadId: tid });
    event('error', keyTab.id, { error });
    assert.deepEqual(calls.at(-1), ['type', 'Hallo Quest', { id: typeA.id, enter: false }], error);
  }
});

await ok('the watchdog settles a row the shell never answered; a late event still counts', async () => {
  const calls = installShell();
  // Fake timers: the watchdog fires when the test says.
  const realSet = globalThis.setTimeout;
  const realClear = globalThis.clearTimeout;
  const timers = [];
  globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  globalThis.clearTimeout = () => {};
  const rows = new Map();
  const onState = (id) => (out) => rows.set(id, out);
  const withEnter = { ...typeA, enter: true };
  try {
    kbd.run(withEnter, { threadId: tid, onState: onState(typeA.id) });
    kbd.run(keyB, { threadId: tid, onState: onState(keyB.id) });
    assert.equal(timers.length, 1);
    assert.equal(timers[0].ms, 30000 + 100 * withEnter.text.length, 'room for the phone to ask (25 s) and type');
    assert.equal(kbd.isActive(typeA.id), true);
    assert.deepEqual(rows.get(typeA.id), { state: 'pending', confirm: true });
    // The final event never comes: the row does not wait on with a dead Stop.
    timers[0].fn();
    assert.deepEqual(rows.get(typeA.id), { state: 'unanswered' });
    assert.equal(kbd.isActive(typeA.id), false, 'nothing left for Stop to stop: Retype is free');
    assert.notEqual(rows.get(typeA.id).state, 'pending');
    assert.deepEqual(kbd.describeOutcome(rows.get(typeA.id)),
      ['No answer from the Ara app — check the device before you retype', '']);
    assert.deepEqual(calls.at(-1), ['key', 'enter', { id: keyB.id }], 'the next one starts');
    assert.equal(timers[1].ms, 15000, 'a key: room for the phone to ask (12 s)');
    // A watchdog that fires after its action ended changes nothing.
    event('typed', keyB.id, { count: 1 });
    timers[1].fn();
    assert.equal(rows.get(keyB.id).state, 'pending', 'the view takes the final event itself');
    assert.equal(kbd.isRunning(), false);
    // A password dialog that does not answer Stop is given up on the same way.
    kbd.run(secretC, { threadId: tid, onState: onState(secretC.id) });
    kbd.cancel();
    assert.equal(timers.at(-1).ms, 5000);
    timers.at(-1).fn();
    assert.deepEqual(rows.get(secretC.id), { state: 'unanswered' });
    assert.equal(kbd.isRunning(), false);
  } finally {
    globalThis.setTimeout = realSet;
    globalThis.clearTimeout = realClear;
  }
  // The shell's answer, late: it still reaches the gateway. A watchdog says
  // nothing to the gateway itself — nobody knows what the device took.
  event('typed', typeA.id, { count: 11 });
  await tick();
  assert.deepEqual(posted.map(([url, body]) => [url.split('/')[4], body]), [
    [keyB.id, { state: 'typed' }],
    [typeA.id, { state: 'typed' }],
  ]);
});

await ok('a plain text the phone asked about anyway ends as cancelled like any other', async () => {
  // After the Stop on the phone's strip, or on a device set to ask every
  // time, the phone asks about plain one-line text too, an automatic run
  // included; declined or let run out, it ends as `cancelled` up to 25 s
  // later. The row cannot know, so it says "Typing…"; the watchdog leaves
  // room for that confirmation; the end is reported and worded as any
  // cancelled, Retype is free, and the text never runs by itself again.
  store.clear();
  const calls = installShell();
  const realSet = globalThis.setTimeout;
  const realClear = globalThis.clearTimeout;
  const timers = [];
  globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  globalThis.clearTimeout = () => {};
  const rows = new Map();
  const onState = (id) => (out) => rows.set(id, out);
  const msg = { ts: new Date(now - 1000).toISOString() };
  try {
    assert.equal(kbd.shouldAutoRun(typeA, msg, { live: true, now }), true);
    assert.equal(kbd.claim(typeA.id), true);
    kbd.run(typeA, { threadId: tid, message: msg, onState: onState(typeA.id), now });
    kbd.run(keyTab, { threadId: tid, onState: onState(keyTab.id) });
    assert.deepEqual(rows.get(typeA.id), { state: 'pending' }, 'no confirm mark: the page cannot know');
    assert.deepEqual(kbd.describeOutcome(rows.get(typeA.id), 'Quest 3'), ['Typing on Quest 3…', '']);
    assert.ok(timers[0].ms > 25000, "the watchdog outlasts the phone's 25 s");
    event('error', typeA.id, { error: 'cancelled' });
  } finally {
    globalThis.setTimeout = realSet;
    globalThis.clearTimeout = realClear;
  }
  // The view takes the final event itself (_onKeyboardEvent) and words it.
  assert.deepEqual(kbd.describeOutcome({ state: 'error', error: 'cancelled' }),
    ['Not typed: stopped, or not confirmed on the phone', '']);
  assert.deepEqual(rows.get(keyTab.id), { state: 'error', error: 'cancelled' }, 'what waited is dropped');
  assert.equal(calls.filter((c) => c[0] === 'key').length, 0);
  assert.equal(kbd.isRunning(), false);
  assert.equal(kbd.isActive(typeA.id), false, 'Retype is free');
  assert.equal(kbd.shouldAutoRun(typeA, msg, { live: true, now }), false, 'never by itself again');
  await tick();
  assert.deepEqual(posted.map(([url, body]) => [url.split('/')[4], body]).sort(), [
    [typeA.id, { state: 'error', error: 'cancelled' }],
    [keyTab.id, { state: 'error', error: 'cancelled' }],
  ].sort());
  // A Retype is a deliberate tap and runs as before.
  assert.equal(kbd.run(typeA, { threadId: tid, message: msg, now }), true);
  assert.deepEqual(calls.at(-1), ['type', 'Hallo Quest', { id: typeA.id, enter: false }]);
});

await ok('a tap shows what it types: the text, and every Enter it presses', () => {
  assert.equal(kbd.visibleText({ ...typeA, text: 'ls -la\nrm x\tz' }), 'ls -la⏎\nrm x⇥z');
  assert.equal(kbd.visibleText({ ...typeA, sensitive: true }), null, 'never a sensitive text');
  assert.equal(kbd.visibleText(keyB), null);
  assert.equal(kbd.enterCount({ ...typeA, text: 'a\nb\r\nc', enter: true }), 3);
  assert.equal(kbd.enterCount(keyB), 1);
  assert.equal(kbd.enterCount(typeA), 0);
  assert.equal(kbd.describeAction({ ...typeA, text: 'ls -la\n' }), 'Type 7 characters + Enter',
    'a line break says Enter even without the flag');
  assert.equal(kbd.describeAction({ ...typeA, text: 'a\nb', enter: true }), 'Type 3 characters + Enter ×2');
});

await ok('Stop closes an open password dialog, and the queue moves on', async () => {
  const calls = installShell();
  kbd.run(secretC, { threadId: tid });
  kbd.run(typeA, { threadId: tid });
  assert.equal(kbd.cancel(), true);
  assert.deepEqual(calls.at(-1), ['cancel']);
  event('secret-cancelled', secretC.id);
  assert.equal(kbd.isRunning(), false, 'what waited was dropped, the dialog answered');
});

await ok('a password dialog cancelled on the phone drops the rest too', async () => {
  // Ara's password, then a Tab (or a text) meant to follow it. The user
  // closes the native dialog on the phone (Cancel, back, or the dialog's own
  // ten-minute close, which the shell answers the same way): the password
  // field on the device is still empty, so nothing that waited may run.
  for (const next of [keyTab, typeA]) {
    kbd._reset();
    posted.length = 0;
    const calls = installShell();
    const rows = new Map();
    const onState = (id) => (out) => rows.set(id, out);
    kbd.run(secretC, { threadId: tid, onState: onState(secretC.id) });
    kbd.run(next, { threadId: tid, onState: onState(next.id) });
    assert.deepEqual(rows.get(next.id), { state: 'queued' }, next.kind);
    event('secret-cancelled', secretC.id);
    assert.deepEqual(calls.map((c) => c[0]), ['secret'], `${next.kind}: no further shell call`);
    assert.deepEqual(rows.get(next.id), { state: 'error', error: 'cancelled' }, next.kind);
    assert.equal(kbd.isRunning(), false, next.kind);
    await tick();
    assert.deepEqual(posted.map(([url, body]) => [url.split('/')[4], body]).sort(), [
      [secretC.id, { state: 'secret-cancelled' }],
      [next.id, { state: 'error', error: 'cancelled' }],
    ].sort(), next.kind);
  }
  // A password typed lets the next one run, as it was meant to.
  kbd._reset();
  const calls = installShell();
  kbd.run(secretC, { threadId: tid });
  kbd.run(keyTab, { threadId: tid });
  event('secret-typed', secretC.id);
  assert.deepEqual(calls.at(-1), ['key', 'tab', { id: keyTab.id }]);
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
  assert.equal(kbd.describeAction(secretC), 'Password from manager: Meta account + Enter',
    'the Enter after a password from the manager is shown too');
  assert.equal(kbd.describeAction({ ...secretC, enter: false }), 'Password from manager: Meta account');
  assert.deepEqual(kbd.describeOutcome({ state: 'typed' }, 'Quest 3'), ['Typed on Quest 3 ✓', '']);
  assert.deepEqual(kbd.describeOutcome({ state: 'queued' }), ['Waiting for the one before…', '']);
  // Each fix where it can be made: the keyboard screen connects a device;
  // not-enabled may be this server's switch, which the app's settings page
  // holds (it refuses its keyboard screen meanwhile), or the keyboard itself.
  assert.deepEqual(kbd.describeOutcome({ state: 'error', error: 'not-connected' }), ['Not typed: no device connected', 'keyboard']);
  assert.deepEqual(kbd.describeOutcome({ state: 'error', error: 'not-enabled' }),
    ['Not typed: typing is off in the Ara app (for this server, or the keyboard itself)', 'settings']);
  assert.deepEqual(kbd.describeOutcome({ state: 'error', error: 'not-in-front' }), ['Not typed: the Ara app was not in front', '']);
  // `cancelled` is also a confirmation on the phone declined or not answered in time.
  assert.deepEqual(kbd.describeOutcome({ state: 'error', error: 'cancelled' }),
    ['Not typed: stopped, or not confirmed on the phone', '']);
  assert.deepEqual(kbd.describeOutcome({ state: 'secret-cancelled' }), ['Password not typed: cancelled', '']);
  assert.deepEqual(kbd.describeOutcome(null), ['', '']);
  assert.deepEqual(kbd.describeOutcome({ state: 'unanswered' }),
    ['No answer from the Ara app — check the device before you retype', '']);
});

await ok('a sensitive text is masked, gone once purged, and gone from the page after two minutes', async () => {
  const calls = installShell();
  const sens = { kind: 'keyboard-type', id: 'e'.repeat(32), text: 'hunter2', enter: true, sensitive: true };
  assert.equal(kbd.isSensitive(sens), true);
  assert.equal(kbd.describeAction(sens), 'Type •••••• + Enter');
  assert.ok(!kbd.describeAction(sens).includes('hunter2'));
  // It still types the real text (the phone needs it), on a tap.
  assert.equal(kbd.run(sens, { message: { ts: new Date().toISOString() } }), true);
  assert.deepEqual(calls.pop(), ['type', 'hunter2', { id: sens.id, enter: true }]);
  kbd._reset();
  // Purged: nothing left to type or auto-run.
  const purged = { ...sens, text: null, purged: true };
  assert.equal(kbd.isPurged(purged), true);
  assert.equal(kbd.run(purged), false);
  assert.equal(kbd.shouldAutoRun(purged, fresh, { live: true, now }), false);
  assert.equal(kbd.describeAction({ ...typeA, text: null, purged: true }), 'Text deleted');
  assert.deepEqual(kbd.describeOutcome({ state: 'deleted' }), ['Text deleted', '']);
  // Past two minutes the page treats it as deleted, whatever the gateway has said yet.
  const old = { ts: new Date(now - kbd.SENSITIVE_TTL_MS - 1000).toISOString(), shell_actions: [{ ...sens }, { ...typeA }] };
  assert.equal(kbd.isPurged(old.shell_actions[0], old, now), true);
  assert.equal(kbd.run(old.shell_actions[0], { message: old, now }), false, 'no Retype past the deadline');
  assert.equal(kbd.expireSensitive(old, now), true);
  assert.equal(old.shell_actions[0].text, null, 'the page drops its copy');
  assert.equal(old.shell_actions[1].text, 'Hallo Quest', 'a plain text stays');
  const young = { ts: new Date(now - 30000).toISOString(), shell_actions: [{ ...sens }] };
  assert.equal(kbd.expireSensitive(young, now), false);
  assert.equal(kbd.isPurged(young.shell_actions[0], young, now), false);
});

await ok('reportOutcome posts final states only; the stored outcome comes back', async () => {
  const sent = [];
  const fakeFetch = async (url, opts) => { sent.push([url, opts.method, JSON.parse(opts.body)]); return { ok: true }; };
  assert.equal(await kbd.reportOutcome(tid, typeA, { state: 'typed', count: 11 }, fakeFetch), true);
  assert.equal(await kbd.reportOutcome(tid, typeA, { state: 'status' }, fakeFetch), false, 'not final');
  assert.equal(await kbd.reportOutcome(tid, typeA, { state: 'pending' }, fakeFetch), false);
  assert.deepEqual(sent, [[`/conversations/${tid}/shell-actions/${typeA.id}/outcome`, 'POST', { state: 'typed' }]]);
  const failing = async () => { throw new Error('offline'); };
  assert.equal(await kbd.reportOutcome(tid, typeA, { state: 'typed' }, failing), false);
  assert.deepEqual(kbd.storedOutcome({ ...typeA, outcome: { state: 'typed', at: 'x' } }), { state: 'typed', error: undefined });
  assert.equal(kbd.storedOutcome({ ...typeA, outcome: { state: 'bogus' } }), null);
  assert.equal(kbd.storedOutcome(typeA), null);
});

kbd._reset();
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
            fh.write(HARNESS.replace("__PY_WHITESPACE__", json.dumps(PY_WHITESPACE)))
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stdout.write(proc.stderr)
        print("FAIL: shell-keyboard behaviour")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
