#!/usr/bin/env python3
"""A chamber app's reminder push in the dashboard's service worker (webapp/sw.js).

The gateway pushes a reminder with its app's deep link, two actions and the
API path they post to (docs/chamber-apps.md, "Reminders"). The test runs the
worker's push and notificationclick handlers under Node with a stubbed
registration, clients and network, and checks: the notification carries
*snooze* and *skip* and nothing else a payload names; an action posts to
<api>/snooze or <api>/skip on this origin and opens nothing; a plain tap opens
the app's deep link; an API path that is not a reminder's (another page of the
origin, another origin) gets no actions and posts nothing; an ordinary push is
unchanged.

Needs `node` on PATH; without it the test reports a skip and passes.

    python3 tests/test_webapp_sw_reminders.py
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

const handlers = {};
const shown = [];
const fetched = [];
const opened = [];
const self = {
  addEventListener: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); },
  registration: { showNotification: (title, options) => { shown.push({ title, options }); return Promise.resolve(); } },
  clients: { matchAll: () => Promise.resolve([]), openWindow: (url) => { opened.push(url); return Promise.resolve(); },
             claim: () => Promise.resolve() },
  skipWaiting: () => Promise.resolve(),
};
const ctx = vm.createContext({
  self, location: new URL('https://dash.example/sw.js'), URL, console, setTimeout, clearTimeout,
  caches: { open: () => Promise.resolve({ addAll: () => Promise.resolve() }), keys: () => Promise.resolve([]) },
  fetch: (url, init) => { fetched.push({ url, init }); return Promise.resolve({ ok: true }); },
  indexedDB: { open: () => ({}) }, Promise, JSON, Array, Object, String, Map, Set,
});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);

async function fire(type, event) {
  let waits = [];
  event.waitUntil = (p) => waits.push(p);
  for (const fn of handlers[type] || []) fn(event);
  await Promise.all(waits);
}
const push = (payload) => fire('push', { data: { json: () => payload, text: () => JSON.stringify(payload) } });
const click = (n, action = '') => fire('notificationclick', { action, notification: { data: n.options.data, close() {} } });
const reset = () => { shown.length = 0; fetched.length = 0; opened.length = 0; };

const API = '/apps/api/schedule/routines/routine/hand-1';
const URL_ = '/apps/routines/routine/?retinue-reminder=hand-1#session/hand-1';
await push({ title: 'Hand session', body: 'b', url: URL_, tag: 'reminder:x', kind: 'reminder', api: API,
             occurrence: '2026-11-24T09:30:00Z',
             actions: [{ action: 'snooze', title: 'In 15 min' }, { action: 'skip', title: 'Skip today' },
                       { action: 'approve', title: 'Approve' }] });
assert.equal(shown.length, 1);
assert.equal(shown[0].options.actions.map((a) => a.action).join(), 'snooze,skip');
assert.equal(shown[0].options.data.api, API);
console.log('ok: a reminder shows In 15 min and Skip today, nothing else a payload names');
const note = shown[0];

for (const action of ['snooze', 'skip']) {
  reset();
  await click(note, action);
  assert.equal(fetched.length, 1);
  assert.equal(fetched[0].url, `${API}/${action}`);
  assert.equal(fetched[0].init.method, 'POST');
  assert.equal(fetched[0].init.credentials, 'same-origin');
  assert.equal(opened.length, 0);
}
console.log('ok: snooze and skip post to the reminder\'s API and open nothing');

reset();
await click(note, '');
assert.equal(opened.join(), URL_);
assert.equal(fetched.length, 0);
console.log('ok: a tap opens the app at its deep link');

for (const api of ['/sends/approve/1', 'https://evil.example/apps/api/schedule/a/b/c', '/apps/api/schedule/a/../b/c',
                   '/apps/api/schedule/a/b/c/../../x']) {
  reset();
  await push({ title: 't', url: '/', kind: 'reminder', api, actions: [{ action: 'snooze', title: 's' }] });
  assert.equal(shown[0].options.actions, undefined, api);
  await click(shown[0], 'snooze');
  assert.equal(fetched.length, 0, api);
}
console.log('ok: an API path that is not a reminder\'s gets no actions and posts nothing');

reset();
await push({ title: 'Thread', body: 'x', url: '/#conversation-abc', tag: 'abc' });
assert.equal(shown[0].options.actions, undefined);
assert.equal(JSON.stringify(shown[0].options.data), JSON.stringify({ url: '/#conversation-abc' }));
console.log('ok: an ordinary push is unchanged');
console.log('All service worker reminder checks passed.');
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("skip: node is not on PATH")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        harness = os.path.join(tmp, "harness.mjs")
        with open(harness, "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, harness, SW_JS], capture_output=True, text=True, timeout=60)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
