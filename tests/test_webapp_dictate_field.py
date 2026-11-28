#!/usr/bin/env python3
"""The shared dictation button (webapp/components/dictate-field.js), under Node.

What a dictation into a field does, and what it must never do:

  - the words go where the user's caret or selection was; a selection that
    reaches the end of the field (or spans all of it) is replaced, not
    appended to;
  - only the field's `input` event fires, never `change`: a host that saves
    on `change` (the instance name, a new sphere) must not write a
    transcript the user has not read;
  - leaving the page while the microphone is still being asked for cancels
    the take: the stream that arrives afterwards is stopped at once and no
    recorder starts;
  - leaving mid-take stops the recorder and hands the take to the worker
    only once its last slice is in, so a take shorter than one slice is not
    lost and the last slice of a longer one is not dropped;
  - a mail being written keys its fields by its mailbox's slot
    (compose-sheet.js `_dictKey`), so a transcription still running when From
    changes cannot land in another mailbox's mail.

The service-worker side (dictation.js) and voice.js are stubbed: this pins the
module's own behaviour. Standalone like the rest of the suite; needs `node`.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(HERE, "..", "webapp", "components")

VOICE_STUB = r"""
export function canRecord() { return true; }
export function micErrorText(err) { return `mic: ${err && err.message}`; }
"""

DICTATION_STUB = r"""
export const calls = { transcribe: [], handOff: [], adopt: [] };
export let nextText = 'there';
export function setNext(t) { nextText = t; }
export function transcribe(key, blob, url) {
  calls.transcribe.push({ key, blob, url });
  return Promise.resolve(nextText);
}
export function handOff(key, blob, url) { calls.handOff.push({ key, blob, url }); return true; }
export function adopt(key, onJob) { calls.adopt.push(key); }
"""

HARNESS = r"""
import assert from 'node:assert/strict';

// ── A browser, as far as the module needs one ────────────────────────────────
const winListeners = {};
globalThis.addEventListener = (type, fn) => { (winListeners[type] ||= []).push(fn); };
const fire = (type) => (winListeners[type] || []).forEach((fn) => fn({ type }));

let gumResolve = null;
let tracksStopped = 0;
const stream = () => ({ getTracks: () => [{ stop: () => { tracksStopped += 1; } }] });
let gumMode = 'now';
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: () => (gumMode === 'now'
  ? Promise.resolve(stream())
  : new Promise((r) => { gumResolve = () => r(stream()); })) } } });

let recorders = [];
class FakeRecorder {
  constructor(s) { this.stream = s; this.state = 'inactive'; this.mimeType = 'audio/webm'; this.l = {}; this.held = []; recorders.push(this); }
  addEventListener(t, fn) { (this.l[t] ||= []).push(fn); }
  emit(t, e = {}) { (this.l[t] || []).forEach((fn) => fn(e)); }
  start() { this.state = 'recording'; }
  // A slice the recorder holds until it is asked for it or stopped.
  hold(text) { this.held.push(new Blob([text], { type: 'audio/webm' })); }
  slice() { const b = this.held.shift(); if (b) this.emit('dataavailable', { data: b }); }
  stop() {
    if (this.state === 'inactive') throw new Error('inactive');
    this.state = 'inactive';
    // As a browser does: asynchronously, the last slice first, then `stop`.
    setTimeout(() => { while (this.held.length) this.slice(); this.emit('stop'); }, 0);
  }
  requestData() { setTimeout(() => this.slice(), 0); }
}
globalThis.MediaRecorder = FakeRecorder;
globalThis.window = globalThis;

const tick = () => new Promise((r) => setTimeout(r, 5));

// A field and its button in a root, matched by the selectors the module uses.
function makeField(key, value, tag = 'TEXTAREA') {
  const events = [];
  const attrs = { 'data-dictate-field': key };
  return {
    tagName: tag, value, events, selectionStart: value.length, selectionEnd: value.length,
    getAttribute: (a) => (a in attrs ? attrs[a] : null),
    hasAttribute: (a) => a in attrs,
    setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
    dispatchEvent(e) { events.push(e.type); return true; },
  };
}
function makeRoot(fields) {
  const l = {};
  const wraps = fields.map((f) => ({ key: f.getAttribute('data-dictate-field'), innerHTML: '',
    getAttribute: (a) => (a === 'data-dictate-label' ? 'Dictate' : null) }));
  const unq = (v) => v.replace(/\\(.)/g, '$1');
  return {
    isConnected: true,
    addEventListener: (t, fn) => { (l[t] ||= []).push(fn); },
    emit: (t, e) => (l[t] || []).forEach((fn) => fn(e)),
    querySelectorAll(sel) {
      let m = /^\[data-dictate-field="(.*)"\]$/.exec(sel);
      if (m) return fields.filter((f) => f.getAttribute('data-dictate-field') === unq(m[1]));
      m = /^\[data-dictate-wrap="(.*)"\]$/.exec(sel);
      if (m) return wraps.filter((w) => w.key === unq(m[1]));
      if (sel === '[data-dictate-field]') return fields;
      return [];
    },
  };
}
const button = (attr, key) => {
  const el = { getAttribute: (a) => (a === attr ? key : null) };
  el.closest = (s) => (s === `[${attr}]` ? el : null);
  return el;
};
const click = (root, attr, key) => root.emit('click', { target: button(attr, key), preventDefault() {}, stopPropagation() {} });
const leaveField = (root, f, start, end) => { f.selectionStart = start; f.selectionEnd = end; root.emit('focusout', { target: f }); };

const stub = await import('./dictation.js');
const { wireDictation } = await import('./dictate-field.js');

let passed = 0;
async function ok(name, fn) { await fn(); passed += 1; console.log(`ok - ${name}`); }

async function take(root, key, slices = ['a']) {
  click(root, 'data-dictate', key);
  await tick();
  const mr = recorders[recorders.length - 1];
  for (const s of slices) { mr.hold(s); mr.slice(); }
  click(root, 'data-dictate', key);
  await tick();
  return mr;
}

await ok('a selection reaching the end of the field is replaced, not appended to', async () => {
  const f = makeField('k1', 'hello world');
  const root = makeRoot([f]);
  wireDictation(root);
  leaveField(root, f, 6, 11);
  stub.setNext('there');
  await take(root, 'k1');
  assert.equal(f.value, 'hello there');
});

await ok('a selection spanning the whole value is replaced', async () => {
  const f = makeField('k2', 'old words');
  const root = makeRoot([f]);
  wireDictation(root);
  leaveField(root, f, 0, 9);
  stub.setNext('new words');
  await take(root, 'k2');
  assert.equal(f.value, 'new words');
});

await ok('a caret in the middle still inserts there, and one at the end appends', async () => {
  const f = makeField('k3', 'ab cd');
  const root = makeRoot([f]);
  wireDictation(root);
  leaveField(root, f, 2, 2);
  stub.setNext('X');
  await take(root, 'k3');
  assert.equal(f.value, 'ab X cd');
  const g = makeField('k3b', 'one');
  const r2 = makeRoot([g]);
  wireDictation(r2);
  leaveField(r2, g, 3, 3);
  stub.setNext('two');
  await take(r2, 'k3b');
  assert.equal(g.value, 'one two');
});

await ok('a dictation fires input only, never change', async () => {
  const f = makeField('k4', '', 'INPUT');
  const root = makeRoot([f]);
  wireDictation(root);
  stub.setNext('Family.');
  await take(root, 'k4');
  assert.equal(f.value, 'Family', 'a one-line field loses the full stop');
  assert.deepEqual(f.events, ['input']);
});

await ok('leaving while the microphone is being asked for cancels the take', async () => {
  const f = makeField('k5', '');
  const root = makeRoot([f]);
  wireDictation(root);
  gumMode = 'pending';
  const before = recorders.length;
  tracksStopped = 0;
  click(root, 'data-dictate', 'k5');
  await tick();
  fire('pagehide');
  gumResolve();
  await tick();
  assert.equal(recorders.length, before, 'no recorder is started');
  assert.equal(tracksStopped, 1, 'the microphone goes off at once');
  gumMode = 'now';
  // The next take works as ever: the cancelled one holds nothing.
  stub.setNext('fine');
  await take(root, 'k5');
  assert.equal(f.value, 'fine');
});

await ok('leaving mid-take hands the whole take over, its last slice included', async () => {
  const f = makeField('k6', '');
  const root = makeRoot([f]);
  wireDictation(root);
  const t0 = stub.calls.transcribe.length;
  // A take shorter than one slice: nothing has arrived yet.
  click(root, 'data-dictate', 'k6');
  await tick();
  let mr = recorders[recorders.length - 1];
  mr.hold('short');
  fire('pagehide');
  await tick();
  let h = stub.calls.handOff.pop();
  assert.ok(h, 'a short take is handed off');
  assert.equal(h.key, 'k6');
  assert.equal(await h.blob.text(), 'short');
  // A longer one: the slices so far and the one still in the recorder.
  click(root, 'data-dictate', 'k6');
  await tick();
  mr = recorders[recorders.length - 1];
  mr.hold('one '); mr.slice();
  mr.hold('two');
  fire('pagehide');
  await tick();
  h = stub.calls.handOff.pop();
  assert.equal(await h.blob.text(), 'one two');
  assert.equal(stub.calls.handOff.length, 0, 'once');
  assert.equal(stub.calls.transcribe.length, t0, 'the page does not transcribe it too');
  assert.equal(f.value, '');
});

console.log(`${passed} checks passed`);
"""


def compose_keys_by_slot():
    """compose-sheet.js keys every dictation by the mail's slot, not the channel."""
    with open(os.path.join(COMPONENTS, "compose-sheet.js"), encoding="utf-8") as fh:
        src = fh.read()
    keys = re.findall(r"data-dictate-field=\"\$\{esc\(this\.(\w+)\)\}", src)
    buttons = re.findall(r"dictateButtonHtml\(`\$\{this\.(\w+)\}", src)
    if len(keys) < 3 or len(buttons) < 3:
        return f"expected the subject, text and recipient fields, found {keys} / {buttons}"
    if set(keys) | set(buttons) != {"_dictKey"}:
        return f"dictation keyed by {sorted(set(keys) | set(buttons))}, not _dictKey"
    if not re.search(r"get _dictKey\(\) \{ return this\._slotKey\(this\._account\); \}", src):
        return "_dictKey is not the current mailbox's slot"
    return ""


def main() -> int:
    wrong = compose_keys_by_slot()
    if wrong:
        print("FAIL: compose-sheet.js: " + wrong)
        return 1
    print("ok - compose-sheet.js keys dictation by the mailbox's slot")
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the dictation field test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(os.path.join(COMPONENTS, "dictate-field.js"), os.path.join(tmp, "dictate-field.js"))
        for name, body in (("voice.js", VOICE_STUB), ("dictation.js", DICTATION_STUB),
                           ("harness.mjs", HARNESS), ("package.json", '{"type":"module"}')):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True, timeout=120)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: dictation field test")
            return 1
    print("PASS: dictation field test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
