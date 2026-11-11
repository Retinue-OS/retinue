#!/usr/bin/env python3
"""Behavioural tests for the thread list widget (webapp/components/thread-list.js).

The project page lists a project's threads under two tabs, Active and
Archived, so an archived discussion stays one tap away. These checks pin the
widget's contract under Node with a scripted DOM:

- no threads, no widget;
- both tabs are rendered with their counts, and both panels are in the markup
  — the one not showing is `hidden`;
- the widget opens on Active, or on Archived when nothing is active, and a
  tab the user chose wins over that default on every later render;
- a tab press flips the panels in place and records the choice in the host's
  state, without asking the host to re-render;
- titles are escaped.

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
// base.js defines a card element at import; the widget needs none of it.
globalThis.HTMLElement = class {};
const { threadListHtml, threadListTab, wireThreadList } = await import('./thread-list.js');

let passed = 0;
const ok = (name, fn) => { fn(); passed += 1; };

const active = [{ id: 'a', title: 'Quote' }, { id: 'b', title: 'Insurance' }];
const archived = [{ id: 'c', title: 'Inspection', archived: true }];
const hidden = (html, tab) =>
  new RegExp(`data-thread-panel="${tab}" hidden`).test(html);

ok('no threads, no widget', () => {
  assert.equal(threadListHtml([], {}), '');
  assert.equal(threadListHtml(undefined, {}), '');
});

ok('both tabs carry their counts; the archived panel waits hidden', () => {
  const html = threadListHtml([...active, ...archived], {});
  assert.match(html, /data-thread-tab="active">Active <span class="thread-count">2</);
  assert.match(html, /data-thread-tab="archived">Archived <span class="thread-count">1</);
  assert.ok(!hidden(html, 'active'));
  assert.ok(hidden(html, 'archived'));
  assert.match(html, /#conversation-c"/, 'archived rows are in the markup');
});

ok('only archived threads: the widget opens on Archived', () => {
  assert.equal(threadListTab(archived, {}), 'archived');
  const html = threadListHtml(archived, {});
  assert.ok(hidden(html, 'active'));
  assert.ok(!hidden(html, 'archived'));
  assert.match(html, /No active threads\./);
});

ok('a chosen tab wins over the default', () => {
  assert.equal(threadListTab(archived, { tab: 'active' }), 'active');
  assert.ok(hidden(threadListHtml([...active, ...archived], { tab: 'archived' }), 'active'));
});

ok('titles are escaped', () => {
  const html = threadListHtml([{ id: 'x', title: '<b>x</b>' }], {});
  assert.ok(!html.includes('<b>x</b>'));
});

ok('a tab press flips the panels in place and records the choice', () => {
  const el = (attr, value) => {
    const classes = new Set();
    return {
      hidden: false, attrs: { [attr]: value }, classes, listeners: [],
      getAttribute(n) { return this.attrs[n]; },
      setAttribute(n, v) { this.attrs[n] = v; },
      addEventListener(_t, f) { this.listeners.push(f); },
      classList: { toggle(c, on) { if (on) classes.add(c); else classes.delete(c); } },
    };
  };
  const tabs = [el('data-thread-tab', 'active'), el('data-thread-tab', 'archived')];
  const panels = [el('data-thread-panel', 'active'), el('data-thread-panel', 'archived')];
  panels[1].hidden = true;
  const root = { querySelectorAll: (sel) => (sel.includes('panel') ? panels : tabs) };
  const state = {};
  wireThreadList(root, state);
  tabs[1].listeners[0]();
  assert.equal(state.tab, 'archived');
  assert.equal(panels[0].hidden, true);
  assert.equal(panels[1].hidden, false);
  assert.ok(tabs[1].classes.has('on') && !tabs[0].classes.has('on'));
  assert.equal(tabs[1].attrs['aria-selected'], 'true');
  assert.equal(tabs[0].attrs['aria-selected'], 'false');
});

console.log(`${passed} checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the thread list test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("base.js", "device-store.js", "thread-list.js"):
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
            print("FAIL: thread list test")
            return 1
    print("PASS: thread list test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
