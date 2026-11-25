#!/usr/bin/env python3
"""The chamber apps' client, webapp/apps/retinue.js (docs/chamber-apps.md).

Runs the classic script under Node with a stubbed page (location, history,
fetch, the Android shell's bridge) and checks: `window.Retinue` exists only
on an app page of the dashboard and names the app; the API calls go to
/apps/api/ with the documented paths and methods; a changed plan tells a
shell that has `remindersChanged`; `speak` and `notify` use the shell's
native members when it has them (feature-detected, RetinueShell/7) and the
browser's otherwise; a page opened from a reminder reports it opened once
and drops the marker from its address; a host that supplied the object
first is left alone.

Needs `node` on PATH; without it the test reports a skip and passes.

    python3 tests/test_webapp_apps_client.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(HERE, "..", "webapp", "apps", "retinue.js")

HARNESS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const src = fs.readFileSync(process.argv[2], 'utf8');

function page(href, { shell = null, preset = undefined, ua = 'Mozilla/5.0' } = {}) {
  const calls = [];
  const listeners = {};
  const win = {
    location: new URL(href),
    history: { state: null, replaceState(_s, _t, url) { win.replaced = url; } },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); },
    dispatch: (t, detail) => (listeners[t] || []).forEach((fn) => fn({ detail })),
    fetch: (url, init) => {
      calls.push({ url, method: init.method, body: init.body });
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{"jobs": [], "thread": "t1"}') });
    },
    navigator: { userAgent: ua },
    Blob, URL, Promise, JSON, Object, Number, String, Math, setTimeout, clearTimeout, TypeError, Error,
    ArrayBuffer, encodeURIComponent, decodeURIComponent,
  };
  if (shell) win.RetinueShell = shell;
  if (preset) win.Retinue = preset;
  win.window = win;
  vm.createContext(win);
  vm.runInContext(src, win);
  return { win, calls };
}
const tick = () => new Promise((r) => setTimeout(r, 0));

{
  const { win } = page('https://dash.example/sends');
  assert.equal(win.Retinue, undefined);
  console.log('ok: no Retinue outside an app page');
}
{
  const { win, calls } = page('https://dash.example/apps/routines/routine/index.html');
  const R = win.Retinue;
  assert.equal(R.app.chamber, 'routines');
  assert.equal(R.app.id, 'routine');
  assert.equal(R.version, 1);
  assert.equal(R.shell, null);
  await R.schedule({ id: 'a' });
  await R.unschedule('a');
  await R.jobs();
  await R.done('a'); await R.skip('a'); await R.snooze('a', 10);
  await R.sparql('SELECT 1');
  await R.submit('log.json', '{}', 'sessions/2026');
  await R.submit('x.json', '{}', undefined, { replace: true });
  assert.equal(await R.ask('coach', 'hi'), 't1');
  const seen = calls.map((c) => `${c.method} ${c.url}`);
  assert.deepEqual(seen, [
    'POST /apps/api/schedule/routines/routine',
    'DELETE /apps/api/schedule/routines/routine/a',
    'GET /apps/api/schedule/routines/routine',
    'POST /apps/api/schedule/routines/routine/a/done',
    'POST /apps/api/schedule/routines/routine/a/skip',
    'POST /apps/api/schedule/routines/routine/a/snooze',
    'POST /apps/api/sparql',
    'PUT /apps/api/files/routines/sessions/2026/log.json',
    'PUT /apps/api/files/routines/x.json?replace=1',
    'POST /apps/api/ask',
  ]);
  assert.equal(JSON.parse(calls[5].body).minutes, 10);
  assert.equal(JSON.parse(calls[9].body).app.chamber, 'routines');
  assert.throws(() => R.done(''), TypeError);
  console.log('ok: the API calls go to /apps/api/ with the documented paths');
}
{
  let changed = 0;
  const spoken = [];
  const notes = [];
  const shell = { version: '1.4.0', platform: 'android', remindersChanged: () => { changed += 1; },
                  speak: (t, l, id) => spoken.push({ t, l, id }), notify: (t, b) => notes.push([t, b]) };
  const { win } = page('https://dash.example/apps/routines/routine/', { shell, ua: 'Mozilla/5.0 RetinueShell/7 (Android)' });
  const R = win.Retinue;
  assert.equal(R.shell.version, 7);
  assert.equal(R.shell.app, '1.4.0');
  await R.schedule({ id: 'a' }); await R.skip('a');
  assert.equal(changed, 2);
  const done = R.speak('Next set', 'en');
  await tick();
  assert.equal(spoken.length, 1);
  assert.equal(spoken[0].l, 'en');
  win.dispatch('retinue-shell', { type: 'speak', id: spoken[0].id, state: 'done' });
  assert.equal(await done, true);
  assert.equal(await R.notify('Title', 'Body'), true);
  assert.deepEqual(notes, [['Title', 'Body']]);
  console.log('ok: inside the shell, speak, notify and remindersChanged use its bridge');
}
{
  const shell = { version: '1.3.0', platform: 'android' };   // an older shell: none of the members
  const { win } = page('https://dash.example/apps/routines/routine/', { shell, ua: 'Mozilla/5.0 RetinueShell/6 (Android)' });
  assert.equal(win.Retinue.shell.version, 6);
  await win.Retinue.schedule({ id: 'a' });               // no remindersChanged: nothing thrown
  assert.equal(await win.Retinue.speak('x'), false);     // no speechSynthesis in this stub
  console.log('ok: an older shell is feature-detected, not assumed');
}
{
  const { win, calls } = page('https://dash.example/apps/routines/routine/?a=1&retinue-reminder=hand-1#session/1');
  await tick();
  assert.equal(win.replaced, '/apps/routines/routine/?a=1#session/1');
  assert.deepEqual(calls.map((c) => `${c.method} ${c.url}`), ['POST /apps/api/schedule/routines/routine/hand-1/opened']);
  console.log('ok: opened from a reminder, it reports it once and drops the marker');
}
{
  const preset = { host: 'extension' };
  const { win } = page('https://dash.example/apps/routines/routine/', { preset });
  assert.equal(win.Retinue, preset);
  console.log('ok: a host that supplied window.Retinue first is left alone');
}
console.log('All apps client checks passed.');
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
        proc = subprocess.run([node, harness, CLIENT], capture_output=True, text=True, timeout=60)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
