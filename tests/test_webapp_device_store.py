#!/usr/bin/env python3
"""Behavioural tests for the dashboard's per-device preferences (webapp/components/device-store.js).

In a browser the voice timeouts, the shelf size and the layout live in
localStorage, per browser and site. Inside the Android app (RetinueShell/4)
the app holds them, so they apply to every endpoint the phone shows. These
checks pin down both paths: the browser fallback, the app-held values, the
one-time hand-over of a value the browser held before, the key allowlist the
app mirrors, the change events, and that continuous.js and shelf-store.js
really go through the store.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.

    python3 tests/test_webapp_device_store.py
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

// ── A browser of sorts: window events and a scripted localStorage ────────────
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

const ds = await import('./device-store.js');
const { voiceTimeouts, setVoiceTimeouts, DEFAULT_SILENCE_MS } = await import('./continuous.js');
const shelf = await import('./shelf-store.js');

let passed = 0;
function ok(name, fn) { fn(); passed++; console.log('  ok  ' + name); }

// The app's members as its page script provides them: a copy the page reads
// synchronously, writes recorded as the messages the app would receive.
function installApp(initial = {}) {
  const values = { ...initial };
  const posts = [];
  window.RetinueShell = {
    platform: 'android',
    deviceSettings: () => JSON.stringify(values),
    setDeviceSetting: (key, value) => {
      posts.push([key, value]);
      if (value == null) delete values[key]; else values[key] = String(value);
    },
  };
  return { values, posts };
}

ok('the allowlist matches what the app keeps', () => {
  for (const k of ['retinue-voice-silence-ms', 'retinue-voice-nospeech-ms', 'retinue.shelf.size',
    'retinue.layout.v1', 'retinue.chatpage.v1', 'retinue.view.projects']) {
    assert.ok(ds.isDeviceKey(k), k);
  }
  for (const k of ['retinue.shelf.v1', 'retinue.view.', 'retinue-draft:x', 'retinue.view.a b', '',
    'retinue.view.' + 'x'.repeat(80), null, 42]) {
    assert.ok(!ds.isDeviceKey(k), String(k));
  }
});

ok('in a browser the values live in localStorage', () => {
  delete window.RetinueShell;
  assert.equal(ds.appHoldsDeviceSettings(), false);
  ds.deviceSet('retinue.shelf.size', 7);
  assert.equal(store.get('retinue.shelf.size'), '7');
  assert.equal(ds.deviceGet('retinue.shelf.size'), '7');
  ds.deviceSet('retinue.shelf.size', null);
  assert.equal(store.has('retinue.shelf.size'), false);
  assert.equal(ds.deviceGet('retinue.shelf.size'), null);
});

ok('inside the app the app holds them, for every endpoint', () => {
  store.clear();
  const app = installApp({ 'retinue.shelf.size': '9' });
  assert.equal(ds.appHoldsDeviceSettings(), true);
  assert.equal(ds.deviceGet('retinue.shelf.size'), '9');
  ds.deviceSet('retinue.view.news', 'list');
  assert.deepEqual(app.posts, [['retinue.view.news', 'list']]);
  assert.equal(store.has('retinue.view.news'), false, 'nothing written locally');
  assert.equal(ds.deviceGet('retinue.view.news'), 'list');
});

ok("a browser's own value is handed to the app once, and only when the app has none", () => {
  store.clear();
  store.set('retinue-voice-silence-ms', '2200');
  store.set('retinue.shelf.size', '3');
  const app = installApp({ 'retinue.shelf.size': '12' });
  // The app has a shelf size already: the local one does not override it.
  assert.equal(ds.deviceGet('retinue.shelf.size'), '12');
  // The app has no silence value: this browser's is handed over, once.
  assert.equal(ds.deviceGet('retinue-voice-silence-ms'), '2200');
  assert.equal(ds.deviceGet('retinue-voice-silence-ms'), '2200');
  assert.deepEqual(app.posts, [['retinue-voice-silence-ms', '2200']]);
  assert.equal(app.values['retinue-voice-silence-ms'], '2200');
  // The local copy stays, for an older app.
  assert.equal(store.get('retinue-voice-silence-ms'), '2200');
});

ok('keys the app does not keep stay local, and so does a value too long for it', () => {
  store.clear();
  const app = installApp();
  ds.deviceSet('retinue.attention.held', '1');
  assert.equal(store.get('retinue.attention.held'), '1');
  ds.deviceSet('retinue.layout.v1', 'x'.repeat(ds.MAX_VALUE_LENGTH + 1));
  assert.ok(store.has('retinue.layout.v1'));
  assert.deepEqual(app.posts, []);
});

ok('a broken answer from the app reads as nothing kept', () => {
  store.clear();
  window.RetinueShell = { deviceSettings: () => 'not json', setDeviceSetting: () => {} };
  assert.equal(ds.deviceGet('retinue.shelf.size'), null);
  window.RetinueShell = { deviceSettings: () => { throw new Error('gone'); }, setDeviceSetting: () => {} };
  assert.equal(ds.deviceGet('retinue.shelf.size'), null);
});

ok('change events: the app says which keys changed; other tabs say through storage', () => {
  const seen = [];
  const off = ds.onDeviceChange((keys) => seen.push(keys));
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'device-settings', keys: ['retinue.shelf.size', 7] } }));
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'device-settings' } }));
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'wake' } }));
  const tab = new Event('storage');
  Object.defineProperty(tab, 'key', { value: 'retinue.view.projects' });
  window.dispatchEvent(tab);
  const other = new Event('storage');
  Object.defineProperty(other, 'key', { value: 'retinue.shelf.v1' });
  window.dispatchEvent(other);
  assert.deepEqual(seen, [['retinue.shelf.size'], [], ['retinue.view.projects']]);
  off();
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'device-settings', keys: [] } }));
  assert.equal(seen.length, 3);
  // Without the tabs: only the app's events.
  const appOnly = [];
  const off2 = ds.onDeviceChange((keys) => appOnly.push(keys), { tabs: false });
  window.dispatchEvent(tab);
  assert.deepEqual(appOnly, []);
  off2();
});

ok('the voice timeouts come from the app, clamped as ever', () => {
  store.clear();
  const app = installApp({ 'retinue-voice-silence-ms': '2500' });
  assert.equal(voiceTimeouts().silenceMs, 2500);
  const after = setVoiceTimeouts({ silenceMs: 99999 });
  assert.equal(after.silenceMs, 5000);
  assert.deepEqual(app.posts.at(-1), ['retinue-voice-silence-ms', '5000']);
  delete window.RetinueShell;
  assert.equal(voiceTimeouts().silenceMs, DEFAULT_SILENCE_MS, 'the browser has none of its own');
});

ok("the shelf's size comes from the app, and a change there makes room at once", () => {
  store.clear();
  const app = installApp({ 'retinue.shelf.size': '4' });
  assert.equal(shelf.shelfSize(), 4);
  assert.equal(shelf.setShelfSize(80), shelf.MAX_SIZE);
  assert.deepEqual(app.posts.at(-1), ['retinue.shelf.size', String(shelf.MAX_SIZE)]);
  assert.equal(store.has('retinue.shelf.size'), false);
  // The app's settings page sets it: subscribers hear of it.
  let heard = 0;
  const off = shelf.subscribe(() => { heard++; });
  app.values['retinue.shelf.size'] = '2';
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'device-settings', keys: ['retinue.shelf.size'] } }));
  assert.ok(heard >= 1);
  assert.equal(shelf.shelfSize(), 2);
  heard = 0;
  window.dispatchEvent(new CustomEvent('retinue-shell', { detail: { type: 'device-settings', keys: ['retinue.view.news'] } }));
  assert.equal(heard, 0, 'another key does not touch the shelf');
  off();
  delete window.RetinueShell;
});

console.log(`${passed} device-store checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the device-store behaviour test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("device-store.js", "continuous.js", "shelf-store.js"):
            shutil.copy(os.path.join(COMPONENTS, name), os.path.join(tmp, name))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stdout.write(proc.stderr)
        print("FAIL: device-store behaviour")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
