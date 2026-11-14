#!/usr/bin/env python3
"""Behavioural tests for the swipe gesture the chats and threads lists share
(webapp/components/swipe-rows.js), and for the star's one effect on a list.

The gesture is driven under Node with a scripted DOM and scripted pointer
events, and pins what a finger feels:

  * a drag is claimed only once it is clearly horizontal — a mostly vertical
    one is left to the page's scroll, and a twitch under 8 px does nothing;
  * a swipe right past min(120 px, 35 % of the row) commits the row's quick
    action (after the slide-out), a shorter one snaps back;
  * a swipe left past half the tray opens it, past its edge the row resists,
    only one tray is open at a time, and a press outside closes it;
  * the click a swipe ends in never opens the row, a tap on an open row
    closes it instead, and a plain tap is a tap;
  * a long press / right-click / context-menu key opens the tray;
  * Delete arms on the first tap and confirms on the second;
  * a rebuild of the list keeps an open tray open and an armed Delete armed;
  * no gesture starts while a write is on the wire, and `dragging` tells a
    list not to re-render under a finger.

Also: starredFirst (base.js) puts starred rows first and keeps each group's
order, and neither list carries a copy of the gesture of its own.

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

// ── A scripted DOM, just what a row of swipe-rows.js touches ─────────────────
class ClassList {
  constructor(names) { this.s = new Set(names); }
  add(c) { this.s.add(c); }
  remove(c) { this.s.delete(c); }
  contains(c) { return this.s.has(c); }
  toggle(c, on) { if (on === undefined) on = !this.s.has(c); if (on) this.s.add(c); else this.s.delete(c); return on; }
}
class El {
  constructor(tag, cls = '', width = 0) {
    this.tag = tag;
    this.classList = new ClassList(cls.split(' ').filter(Boolean));
    this.children = [];
    this.parent = null;
    this.dataset = {};
    this.style = {};
    this.offsetWidth = width;
    this.textContent = '';
    this._l = {};
  }
  append(...kids) { for (const k of kids) { k.parent = this; this.children.push(k); } return this; }
  addEventListener(t, f) { (this._l[t] ||= []).push(f); }
  setPointerCapture() { this.captured = true; }
  focus() { globalThis.focused = this; }
  *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
  _is(part) { return part.startsWith('.') ? this.classList.contains(part.slice(1)) : this.tag === part; }
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/);
    const out = [];
    for (const el of this.walk()) {
      if (!el._is(parts[parts.length - 1])) continue;
      let i = parts.length - 2;
      for (let a = el.parent; a && a !== this && i >= 0; a = a.parent) if (a._is(parts[i])) i -= 1;
      if (i < 0) out.push(el);
    }
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  fire(type, init = {}) {
    const ev = Object.assign({ type, button: 0, pointerId: 1, defaultPrevented: false }, init);
    ev.preventDefault = () => { ev.defaultPrevented = true; };
    for (const f of this._l[type] || []) f(ev);
    return ev;
  }
}
const docListeners = {};
globalThis.document = {
  addEventListener(t, f) { (docListeners[t] ||= []).push(f); },
  removeEventListener(t, f) { docListeners[t] = (docListeners[t] || []).filter((g) => g !== f); },
};
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.HTMLElement = class {};  // base.js defines a card class at import
const pressOutside = (target) => {
  for (const f of docListeners.pointerdown || []) f({ type: 'pointerdown', composedPath: () => [target] });
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const { SwipeRows } = await import('./swipe-rows.js');
const { starredFirst } = await import('./base.js');

const ROW_W = 360;
const TRAY_W = 200;
function list(ids) {
  const host = new El('section', 'host');
  const root = new El('div', 'tabs');
  host.append(root);
  for (const id of ids) {
    const li = new El('div', 'swipe', ROW_W);
    li.dataset.id = id;
    li.dataset.quick = 'archive';
    const tray = new El('div', 'tray', TRAY_W).append(
      new El('button', 'star'), new El('button', 'restore'), new El('button', 'delete'));
    tray.children[2].textContent = 'Delete';
    li.append(new El('div', 'quick archive'), tray, new El('button', 'row'));
    root.append(li);
  }
  return { host, root };
}
const rowOf = (root, id) => root.querySelectorAll('.swipe').find((el) => el.dataset.id === id);
function swipe(li, dx, dy = 0) {
  const row = li.querySelector('.row');
  row.fire('pointerdown', { clientX: 100, clientY: 100 });
  const steps = 6;
  for (let i = 1; i <= steps; i += 1) {
    row.fire('pointermove', { clientX: 100 + (dx * i) / steps, clientY: 100 + (dy * i) / steps });
  }
  row.fire('pointerup', { clientX: 100 + dx, clientY: 100 + dy });
  return row;
}

let passed = 0;
async function check(name, fn) {
  await fn();
  passed += 1;
  console.log('  ok   ' + name);
}

function setup(ids = ['a', 'b', 'c'], opts = {}) {
  const calls = { quick: [], tap: [] };
  const { host, root } = list(ids);
  const swipe = new SwipeRows(host, {
    onQuick: (id, action) => calls.quick.push([id, action]),
    onTap: (id) => calls.tap.push(id),
    isBusy: opts.isBusy || (() => false),
  });
  swipe.wire(root);
  return { swipe, root, host, calls };
}

await check('a twitch under 8 px is nothing', async () => {
  const { root } = setup();
  const li = rowOf(root, 'a');
  const row = li.querySelector('.row');
  row.fire('pointerdown', { clientX: 100, clientY: 100 });
  row.fire('pointermove', { clientX: 105, clientY: 102 });
  assert.equal(row.style.transform, undefined, 'the row has not moved');
  row.fire('pointerup', { clientX: 105, clientY: 102 });
});

await check('a mostly vertical drag is left to the scroll', async () => {
  const { root, calls, swipe } = setup();
  const li = rowOf(root, 'a');
  const row = li.querySelector('.row');
  row.fire('pointerdown', { clientX: 100, clientY: 100 });
  const ev = row.fire('pointermove', { clientX: 110, clientY: 140 });
  assert.equal(ev.defaultPrevented, false, 'the scroll is not prevented');
  assert.equal(swipe.dragging, false, 'the gesture is dropped');
  row.fire('pointermove', { clientX: 300, clientY: 150 });
  assert.equal(row.style.transform, undefined, 'and a later sideways move does not revive it');
  row.fire('pointerup', { clientX: 300, clientY: 150 });
  await sleep(200);
  assert.deepEqual(calls.quick, []);
});

await check('a swipe right past the threshold commits the quick action', async () => {
  const { root, calls, swipe } = setup();
  const li = rowOf(root, 'b');
  const row = li.querySelector('.row');
  row.fire('pointerdown', { clientX: 100, clientY: 100 });
  row.fire('pointermove', { clientX: 130, clientY: 101 });
  assert.equal(swipe.dragging, true, 'a finger is down: no re-render now');
  assert.equal(row.captured, true, 'the pointer is captured once the swipe is claimed');
  row.fire('pointermove', { clientX: 240, clientY: 101 });
  assert.equal(row.style.transform, 'translateX(140px)');
  assert.ok(li.classList.contains('show-quick'), 'the quick action shows under it');
  row.fire('pointerup', { clientX: 240, clientY: 101 });
  assert.equal(swipe.dragging, false);
  assert.equal(row.style.transform, `translateX(${ROW_W}px)`, 'the row slides out');
  assert.deepEqual(calls.quick, [], 'the action waits for the slide');
  await sleep(200);
  assert.deepEqual(calls.quick, [['b', 'archive']]);
});

await check('the threshold is 35 % of a narrow row', async () => {
  const { root, calls } = setup();
  const li = rowOf(root, 'a');
  li.offsetWidth = 200;  // 35 % = 70 px, under the 120 px cap
  swipe(li, 90);
  await sleep(200);
  assert.deepEqual(calls.quick, [['a', 'archive']]);
});

await check('a shorter swipe right snaps back', async () => {
  const { root, calls } = setup();
  const li = rowOf(root, 'a');
  const row = swipe(li, 80);
  assert.equal(row.style.transform, '', 'back at rest');
  assert.ok(!li.classList.contains('show-quick'));
  await sleep(200);
  assert.deepEqual(calls.quick, []);
});

await check('a swipe left past half the tray opens it; past its edge the row resists', async () => {
  const { root, swipe: s } = setup();
  const li = rowOf(root, 'a');
  const row = li.querySelector('.row');
  row.fire('pointerdown', { clientX: 300, clientY: 100 });
  row.fire('pointermove', { clientX: 280, clientY: 100 });
  row.fire('pointermove', { clientX: 300 - TRAY_W - 80, clientY: 100 });
  assert.equal(row.style.transform, `translateX(${-TRAY_W - 20}px)`, 'a quarter of the overshoot');
  row.fire('pointerup', { clientX: 300 - TRAY_W - 80, clientY: 100 });
  assert.equal(s.openId, 'a');
  assert.equal(row.style.transform, `translateX(${-TRAY_W}px)`, 'flush with the tray');
  assert.ok(li.classList.contains('show-tray'));
});

await check('less than half the tray snaps back shut', async () => {
  const { root, swipe: s } = setup();
  const li = rowOf(root, 'a');
  const row = swipe(li, -(TRAY_W / 2 - 10));
  assert.equal(s.openId, null);
  assert.equal(row.style.transform, '');
});

await check('the click a swipe ends in never opens the row; a plain tap does', async () => {
  const { root, calls } = setup();
  const li = rowOf(root, 'a');
  const row = swipe(li, 60);
  const ev = row.fire('click');
  assert.equal(ev.defaultPrevented, true);
  assert.deepEqual(calls.tap, [], 'the swipe opened nothing');
  row.fire('click');
  assert.deepEqual(calls.tap, ['a'], 'the next tap is a tap');
});

await check('a tap on an open row closes it instead of opening it', async () => {
  const { root, calls, swipe: s } = setup();
  const li = rowOf(root, 'a');
  const row = swipe(li, -TRAY_W);
  await sleep(450);  // past the swallow window: this is a fresh tap
  row.fire('click');
  assert.equal(s.openId, null);
  assert.deepEqual(calls.tap, []);
  assert.equal(row.style.transform, '');
});

await check('a tap on another row while a tray is open only closes the tray', async () => {
  const { root, calls, swipe: s } = setup();
  swipe(rowOf(root, 'a'), -TRAY_W);
  await sleep(450);
  rowOf(root, 'b').querySelector('.row').fire('click');
  assert.equal(s.openId, null);
  assert.deepEqual(calls.tap, []);
});

await check('one tray at a time', async () => {
  const { root, swipe: s } = setup();
  swipe(rowOf(root, 'a'), -TRAY_W);
  swipe(rowOf(root, 'b'), -TRAY_W);
  assert.equal(s.openId, 'b');
  assert.equal(rowOf(root, 'a').querySelector('.row').style.transform, '', 'the first one closed');
});

await check('a press outside the list closes the tray', async () => {
  const { root, swipe: s } = setup();
  swipe(rowOf(root, 'c'), -TRAY_W);
  pressOutside(new El('nav'));
  assert.equal(s.openId, null);
});

await check('long press / right-click opens the tray, the keyboard lands on its first button', async () => {
  const { root, swipe: s } = setup();
  const li = rowOf(root, 'b');
  const ev = li.querySelector('.row').fire('contextmenu', { pointerType: '', button: 0 });
  assert.equal(ev.defaultPrevented, true, 'no browser menu');
  assert.equal(s.openId, 'b');
  assert.equal(globalThis.focused, li.querySelector('.tray button'));
});

await check('Delete arms on the first tap and confirms on the second', async () => {
  const { root, swipe: s } = setup();
  const li = rowOf(root, 'a');
  swipe(li, -TRAY_W);
  const del = li.querySelector('.tray .delete');
  assert.equal(s.confirm('a', del), false);
  assert.equal(del.textContent, 'Delete for good?');
  assert.ok(del.classList.contains('confirm'));
  assert.equal(s.confirm('a', del), true);
});

await check('closing disarms an armed Delete', async () => {
  const { root, swipe: s } = setup();
  const li = rowOf(root, 'a');
  swipe(li, -TRAY_W);
  const del = li.querySelector('.tray .delete');
  s.confirm('a', del);
  s.close();
  assert.equal(del.textContent, 'Delete');
  assert.ok(!del.classList.contains('confirm'));
  assert.equal(s.confirm('a', del), false, 'the next tap arms again');
});

await check('a rebuilt list keeps the open tray open and an armed Delete armed', async () => {
  const { host, root, swipe: s } = setup();
  swipe(rowOf(root, 'b'), -TRAY_W);
  s.confirm('b', rowOf(root, 'b').querySelector('.tray .delete'));
  const fresh = list(['a', 'b', 'c']).root;
  host.append(fresh);
  s.wire(fresh);
  const li = rowOf(fresh, 'b');
  assert.equal(s.openId, 'b');
  assert.equal(li.querySelector('.row').style.transition, 'none', 'no replayed slide');
  assert.equal(li.querySelector('.tray .delete').textContent, 'Delete for good?');
});

await check('a rebuilt list without the open row forgets it', async () => {
  const { host, root, swipe: s } = setup();
  swipe(rowOf(root, 'b'), -TRAY_W);
  const fresh = list(['a', 'c']).root;
  host.append(fresh);
  s.wire(fresh);
  assert.equal(s.openId, null);
});

await check('no gesture starts while a write is on the wire', async () => {
  let busy = true;
  const { root, calls, swipe: s } = setup(['a'], { isBusy: () => busy });
  const row = swipe(rowOf(root, 'a'), 200);
  assert.equal(row.style.transform, undefined);
  await sleep(200);
  assert.deepEqual(calls.quick, []);
  busy = false;
  swipe(rowOf(root, 'a'), 200);
  await sleep(200);
  assert.deepEqual(calls.quick, [['a', 'archive']]);
  assert.equal(s.dragging, false);
});

await check('starredFirst: starred rows lead, each group keeps its order', async () => {
  const rows = [{ id: 1 }, { id: 2, starred: true }, { id: 3 }, { id: 4, starred: true }, { id: 5 }];
  assert.deepEqual(starredFirst(rows).map((r) => r.id), [2, 4, 1, 3, 5]);
  assert.deepEqual(starredFirst([]), []);
  assert.deepEqual(starredFirst(null), []);
  assert.deepEqual(rows.map((r) => r.id), [1, 2, 3, 4, 5], 'the input is left alone');
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    # One gesture, not two copies: both lists use the module, and neither
    # carries a drag state machine of its own (the two used to drift).
    for name in ("chats.js", "conversations.js"):
        with open(os.path.join(COMPONENTS, name), encoding="utf-8") as fh:
            src = fh.read()
        if "from './swipe-rows.js'" not in src or "_dragMove" in src:
            print(f"FAIL: {name} must use swipe-rows.js and carry no gesture of its own")
            return 1
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the swipe-rows.js behaviour test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
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
            print("FAIL: swipe-rows.js behaviour test")
            return 1
    print("PASS: swipe-rows.js behaviour test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
