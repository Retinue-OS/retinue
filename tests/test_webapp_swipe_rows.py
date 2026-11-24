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
    list not to re-render under a finger;
  * the chats page's opt-ins leave all of the above alone where a row does
    not carry them: a row with `data-quick-left` reveals its tray on a
    partial swipe left (past half the tray, as any row) and commits that
    action on a full one — past the far threshold, over its own layer, which
    takes over from the tray as the finger crosses it — or on a fast fling
    past the commit threshold; a long press still opens the tray; and a row
    without `data-quick` does not move to the right at all;
  * selection, for a list that opts in (onLongPress / selecting / onSelect):
    a finger held still for LONG_PRESS_MS calls onLongPress instead of
    opening the tray (a moving one does not; right-click and the
    context-menu key still open the tray), the click it ends in is no tap,
    and while selecting a tap or Space calls onSelect and no swipe starts;
    a list that does not opt in (the threads page) keeps the long press's
    tray;
  * the Undo toast (offerUndo): it lapses after UNDO_MS (15 s), stays while
    a finger or the pointer is on it, and a deferred commit is made when it
    lapses, when a newer offer replaces it, or at once with keepalive when
    the page is hidden or left — and never after Undo.

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
// The toast's host and shadow root: enough to read its text, press its
// button, and put a finger on it.
class ToastRoot {
  constructor() { this.html = ''; this.button = null; this.toast = null; }
  set innerHTML(h) {
    this.html = h;
    this.button = h ? new El('button') : null;
    this.toast = h ? new El('div', 'toast') : null;
  }
  get innerHTML() { return this.html; }
  querySelector(sel) { return sel === 'button' ? this.button : sel === '.toast' ? this.toast : null; }
}
const toastRoots = [];
globalThis.document = {
  visibilityState: 'visible',
  hidden: false,
  addEventListener(t, f) { (docListeners[t] ||= []).push(f); },
  removeEventListener(t, f) { docListeners[t] = (docListeners[t] || []).filter((g) => g !== f); },
  createElement() {
    const host = { isConnected: true, setAttribute() {}, attachShadow() { this.shadowRoot = new ToastRoot(); toastRoots.push(this.shadowRoot); return this.shadowRoot; } };
    return host;
  },
  body: { appendChild() {} },
};
const winListeners = {};
globalThis.window = { addEventListener(t, f) { (winListeners[t] ||= []).push(f); } };
const toastRoot = () => toastRoots[toastRoots.length - 1];
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.HTMLElement = class {};  // base.js defines a card class at import
const pressOutside = (target) => {
  for (const f of docListeners.pointerdown || []) f({ type: 'pointerdown', composedPath: () => [target] });
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const { SwipeRows, offerUndo, UNDO_MS, hasPendingCommit, LONG_PRESS_MS } = await import('./swipe-rows.js');
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
// Rows of the chats page's Unread and Read tabs: a committing swipe left
// (data-quick-left), and on Read no swipe right (no data-quick).
function leftList(ids, { quick = 'read' } = {}) {
  const host = new El('section', 'host');
  const root = new El('div', 'tabs');
  host.append(root);
  for (const id of ids) {
    const li = new El('div', 'swipe', ROW_W);
    li.dataset.id = id;
    if (quick) li.dataset.quick = quick;
    li.dataset.quickLeft = 'archive';
    const tray = new El('div', 'tray', TRAY_W).append(new El('button', 'star'), new El('button', 'mute'));
    li.append(new El('div', 'quick read'), new El('div', 'quick-left archive'), tray, new El('button', 'row'));
    root.append(li);
  }
  return { host, root };
}
function setupLeft(ids = ['a', 'b'], opts = {}) {
  const calls = { quick: [], tap: [] };
  const { host, root } = leftList(ids, opts);
  const swipe = new SwipeRows(host, {
    onQuick: (id, action) => calls.quick.push([id, action]),
    onTap: (id) => calls.tap.push(id),
  });
  swipe.wire(root);
  return { swipe, root, host, calls };
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

await check('on a row without the opt-in a swipe left never commits', async () => {
  const { root, calls, swipe: s } = setup();
  swipe(rowOf(root, 'a'), -300);
  await sleep(200);
  assert.deepEqual(calls.quick, [], 'it opened the tray instead');
  assert.equal(s.openId, 'a');
});

// The far threshold of these rows: the tray plus 40 px, at least 60 % of the
// row, at most 85 % of it.
const FAR = Math.min(Math.max(TRAY_W + 40, ROW_W * 0.6), ROW_W * 0.85);

await check('data-quick-left: a partial swipe left reveals the tray, and commits nothing', async () => {
  const { root, calls, swipe: s } = setupLeft();
  const li = rowOf(root, 'b');
  const row = li.querySelector('.row');
  row.fire('pointerdown', { clientX: 300, clientY: 100 });
  row.fire('pointermove', { clientX: 280, clientY: 100 });
  row.fire('pointermove', { clientX: 160, clientY: 100 });
  assert.equal(row.style.transform, 'translateX(-140px)', 'no resistance on this side');
  assert.ok(li.classList.contains('show-tray'), 'the tray shows under it');
  assert.ok(!li.classList.contains('show-quick-left'), 'the commit layer does not, yet');
  row.fire('pointerup', { clientX: 160, clientY: 100 });
  assert.equal(s.openId, 'b', 'the tray is open');
  assert.equal(row.style.transform, `translateX(${-TRAY_W}px)`, 'flush with it');
  await sleep(200);
  assert.deepEqual(calls.quick, [], 'nothing committed');
});

await check('data-quick-left: a full swipe left past the far threshold commits that action', async () => {
  const { root, calls, swipe: s } = setupLeft();
  assert.equal(s.farThreshold(rowOf(root, 'b')), FAR);
  const li = rowOf(root, 'b');
  const row = li.querySelector('.row');
  row.fire('pointerdown', { clientX: 350, clientY: 100 });
  row.fire('pointermove', { clientX: 330, clientY: 100 });
  row.fire('pointermove', { clientX: 350 - FAR + 10, clientY: 100 });
  assert.ok(li.classList.contains('show-tray') && !li.classList.contains('show-quick-left'),
    'short of the far threshold: still the tray');
  row.fire('pointermove', { clientX: 350 - FAR - 20, clientY: 100 });
  assert.ok(li.classList.contains('show-quick-left'), 'past it: the action\'s own layer');
  assert.ok(!li.classList.contains('show-tray'), 'and the tray gives way');
  row.fire('pointerup', { clientX: 350 - FAR - 20, clientY: 100 });
  assert.equal(row.style.transform, `translateX(${-ROW_W}px)`, 'the row slides out to the left');
  assert.equal(s.openId, null, 'no tray opened');
  await sleep(200);
  assert.deepEqual(calls.quick, [['b', 'archive']]);
});

await check('data-quick-left: a fast fling past the commit threshold commits; a slow one reveals', async () => {
  const fling = (li, ms) => {
    const row = li.querySelector('.row');
    row.fire('pointerdown', { clientX: 300, clientY: 100, timeStamp: 1000 });
    for (let i = 1; i <= 5; i += 1) {
      row.fire('pointermove', { clientX: 300 - 30 * i, clientY: 100, timeStamp: 1000 + ms * i });
    }
    row.fire('pointerup', { clientX: 150, clientY: 100, timeStamp: 1000 + ms * 5 });
  };
  const fast = setupLeft();
  fling(rowOf(fast.root, 'a'), 10);  // 3 px/ms, 150 px: short of the far threshold
  await sleep(200);
  assert.deepEqual(fast.calls.quick, [['a', 'archive']]);
  const slow = setupLeft();
  fling(rowOf(slow.root, 'a'), 100);  // 0.3 px/ms
  await sleep(200);
  assert.deepEqual(slow.calls.quick, []);
  assert.equal(slow.swipe.openId, 'a', 'revealed instead');
  // A fling that does not even reach the commit threshold only reveals.
  const short = setupLeft();
  const row = rowOf(short.root, 'a').querySelector('.row');
  row.fire('pointerdown', { clientX: 300, clientY: 100, timeStamp: 0 });
  row.fire('pointermove', { clientX: 250, clientY: 100, timeStamp: 5 });
  row.fire('pointermove', { clientX: 190, clientY: 100, timeStamp: 10 });
  row.fire('pointerup', { clientX: 190, clientY: 100, timeStamp: 10 });
  await sleep(200);
  assert.deepEqual(short.calls.quick, []);
  assert.equal(short.swipe.openId, 'a');
});

await check('data-quick-left: the left side runs to nearly all of the row', async () => {
  const { root } = setupLeft();
  const li = rowOf(root, 'a');
  const row = li.querySelector('.row');
  row.fire('pointerdown', { clientX: 350, clientY: 100 });
  row.fire('pointermove', { clientX: 330, clientY: 100 });
  row.fire('pointermove', { clientX: 0, clientY: 100 });
  assert.equal(row.style.transform, `translateX(${-ROW_W * 0.95}px)`);
});

await check('data-quick-left: a swipe short of half the tray snaps back and opens nothing', async () => {
  const { root, calls, swipe: s } = setupLeft();
  const li = rowOf(root, 'a');
  const row = swipe(li, -(TRAY_W / 2 - 10));
  assert.equal(row.style.transform, '');
  assert.ok(!li.classList.contains('show-quick-left') && !li.classList.contains('show-tray'));
  assert.equal(s.openId, null);
  await sleep(200);
  assert.deepEqual(calls.quick, []);
});

await check('data-quick-left: the right side still commits data-quick', async () => {
  const { root, calls } = setupLeft();
  swipe(rowOf(root, 'a'), 200);
  await sleep(200);
  assert.deepEqual(calls.quick, [['a', 'read']]);
});

await check('data-quick-left: a long press still opens the tray, and a drag then moves the tray', async () => {
  const { root, calls, swipe: s } = setupLeft();
  const li = rowOf(root, 'a');
  const row = li.querySelector('.row');
  row.fire('contextmenu', { pointerType: 'touch', button: 0 });
  assert.equal(s.openId, 'a');
  assert.ok(li.classList.contains('show-tray') && !li.classList.contains('show-quick-left'));
  row.fire('pointerdown', { clientX: 300, clientY: 100 });
  row.fire('pointermove', { clientX: 280, clientY: 100 });
  row.fire('pointermove', { clientX: 220, clientY: 100 });
  assert.equal(row.style.transform, `translateX(${-TRAY_W - 20}px)`, 'past the open tray it resists');
  row.fire('pointerup', { clientX: 220, clientY: 100 });
  assert.equal(s.openId, 'a', 'and the tray stays open');
  await sleep(200);
  assert.deepEqual(calls.quick, [], 'nothing committed from an open tray');
});

await check('no data-quick: the row does not move right and commits nothing there', async () => {
  const { root, calls } = setupLeft(['a'], { quick: '' });
  const li = rowOf(root, 'a');
  const row = li.querySelector('.row');
  row.fire('pointerdown', { clientX: 100, clientY: 100 });
  row.fire('pointermove', { clientX: 120, clientY: 100 });
  row.fire('pointermove', { clientX: 300, clientY: 100 });
  assert.equal(row.style.transform, '', 'held at rest');
  assert.ok(!li.classList.contains('show-quick'));
  row.fire('pointerup', { clientX: 300, clientY: 100 });
  await sleep(200);
  assert.deepEqual(calls.quick, []);
  swipe(li, -(FAR + 20));
  await sleep(200);
  assert.deepEqual(calls.quick, [['a', 'archive']], 'the left side still commits');
});

// ── Selection (the chats page's opt-in) ─────────────────────────────────────
function setupSelect() {
  const calls = { long: [], select: [], tap: [], quick: [] };
  let selecting = false;
  const { host, root } = list(['a', 'b']);
  const s = new SwipeRows(host, {
    onQuick: (id, action) => calls.quick.push([id, action]),
    onTap: (id) => calls.tap.push(id),
    onLongPress: (id) => { calls.long.push(id); selecting = true; },
    selecting: () => selecting,
    onSelect: (id, _el, e) => calls.select.push([id, !!(e && e.shiftKey)]),
  });
  s.wire(root);
  return { s, root, calls, stop: () => { selecting = false; } };
}

await check('a finger held still is a long press: selection, not the tray', async () => {
  const { s, root, calls } = setupSelect();
  const row = rowOf(root, 'a').querySelector('.row');
  row.fire('pointerdown', { clientX: 100, clientY: 100, pointerType: 'touch' });
  await sleep(LONG_PRESS_MS + 60);
  assert.deepEqual(calls.long, ['a']);
  assert.equal(s.openId, null, 'no tray');
  // The browser's own contextmenu for the same press: once only.
  row.fire('contextmenu', { pointerType: 'touch', button: 0 });
  assert.deepEqual(calls.long, ['a']);
  row.fire('pointerup', { clientX: 100, clientY: 100, pointerType: 'touch' });
  const ev = row.fire('click');
  assert.equal(ev.defaultPrevented, true, 'the lifted finger is no tap');
  assert.deepEqual(calls.select, [], 'nor a toggle');
});

await check('a finger that moves is no long press; a mouse right-click still opens the tray', async () => {
  const { s, root, calls } = setupSelect();
  const row = rowOf(root, 'a').querySelector('.row');
  row.fire('pointerdown', { clientX: 100, clientY: 100, pointerType: 'touch' });
  row.fire('pointermove', { clientX: 100, clientY: 130, pointerType: 'touch' });
  await sleep(LONG_PRESS_MS + 60);
  assert.deepEqual(calls.long, []);
  row.fire('pointerup', { clientX: 100, clientY: 130 });
  row.fire('contextmenu', { pointerType: 'mouse', button: 2 });
  assert.equal(s.openId, 'a', 'right-click: the tray');
  assert.deepEqual(calls.long, []);
  s.close();
  // A contextmenu of a touch without the timer having seen it (a browser
  // that says so first) is the long press too.
  rowOf(root, 'b').querySelector('.row').fire('contextmenu', { pointerType: 'touch', button: 0 });
  assert.deepEqual(calls.long, ['b']);
  assert.equal(s.openId, null);
});

await check('while selecting a tap or Space selects, and no swipe starts', async () => {
  const { s, root, calls } = setupSelect();
  rowOf(root, 'b').querySelector('.row').fire('contextmenu', { pointerType: 'touch', button: 0 });
  await sleep(1600);  // past the long press's own click
  const row = rowOf(root, 'a').querySelector('.row');
  const ev = row.fire('click', { shiftKey: true });
  assert.equal(ev.defaultPrevented, true, 'the link is not followed');
  assert.deepEqual(calls.select, [['a', true]]);
  assert.deepEqual(calls.tap, []);
  const key = row.fire('keydown', { key: ' ' });
  assert.equal(key.defaultPrevented, true);
  assert.deepEqual(calls.select, [['a', true], ['a', false]]);
  swipe(rowOf(root, 'a'), 200);
  await sleep(200);
  assert.deepEqual(calls.quick, [], 'no swipe while selecting');
  assert.equal(row.style.transform, undefined);
  assert.equal(s.dragging, false);
});

await check('a list without the opt-in keeps the long press\'s tray', async () => {
  const { root, swipe: s } = setup();
  const li = rowOf(root, 'a');
  li.querySelector('.row').fire('pointerdown', { clientX: 100, clientY: 100, pointerType: 'touch' });
  await sleep(LONG_PRESS_MS + 60);
  assert.equal(s.openId, null, 'no timer of its own');
  li.querySelector('.row').fire('contextmenu', { pointerType: 'touch', button: 0 });
  assert.equal(s.openId, 'a');
  li.querySelector('.row').fire('keydown', { key: ' ' });
});

// ── The Undo toast ──────────────────────────────────────────────────────────
// Timers under test: setTimeout is captured, and time is the tests' to move.
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;
let clock = 0;
let timers = [];
function fakeTime() {
  clock = 0;
  timers = [];
  globalThis.performance = { now: () => clock };
  globalThis.setTimeout = (f, ms) => { const t = { f, at: clock + ms, id: timers.length + 1 }; timers.push(t); return t.id; };
  globalThis.clearTimeout = (id) => { timers = timers.filter((t) => t.id !== id); };
}
function advance(ms) {
  clock += ms;
  for (;;) {
    const due = timers.filter((t) => t.at <= clock).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    timers = timers.filter((t) => t !== due);
    due.f();
  }
}
function realTime() {
  globalThis.setTimeout = realSetTimeout;
  globalThis.clearTimeout = realClearTimeout;
}

await check('the Undo lasts 15 s, and stays while a finger is on it', async () => {
  fakeTime();
  try {
    assert.equal(UNDO_MS, 15000);
    let undone = 0;
    offerUndo('Archived · Mara', () => { undone += 1; });
    const root = toastRoot();
    assert.ok(root.innerHTML.includes('Archived · Mara'));
    advance(14000);
    assert.ok(root.innerHTML, 'still there after 14 s');
    // A finger on it: it does not lapse while held …
    root.toast.fire('pointerdown', { pointerType: 'touch' });
    advance(60000);
    assert.ok(root.innerHTML, 'held: still there a minute later');
    // … and once let go it stays a little longer, then lapses.
    root.toast.fire('pointerup', { pointerType: 'touch' });
    root.toast.fire('pointerleave', { pointerType: 'touch' });
    advance(2000);
    assert.ok(root.innerHTML, 'not gone the moment the finger lifts');
    advance(1000);
    assert.equal(root.innerHTML, '', 'lapsed');
    assert.equal(undone, 0);
    // A mouse resting on it holds it too; a click does not let it go.
    offerUndo('Muted · Mara', () => { undone += 1; });
    const r2 = toastRoot();
    r2.toast.fire('pointerenter', { pointerType: 'mouse' });
    r2.toast.fire('pointerup', { pointerType: 'mouse' });
    advance(30000);
    assert.ok(r2.innerHTML, 'the pointer is still on it');
    r2.button.fire('click');
    assert.equal(undone, 1, 'Undo');
    assert.equal(r2.innerHTML, '');
  } finally {
    realTime();
  }
});

await check('a deferred commit: made when the toast lapses, never after Undo', async () => {
  fakeTime();
  try {
    const made = [];
    let undone = 0;
    offerUndo('Deleted · Mara', () => { undone += 1; }, { commit: (keepalive) => made.push(['a', keepalive]) });
    assert.equal(hasPendingCommit(), true);
    advance(14900);
    assert.deepEqual(made, [], 'nothing is sent inside the window');
    advance(200);
    assert.deepEqual(made, [['a', false]], 'sent once it ends');
    assert.equal(hasPendingCommit(), false);
    advance(60000);
    assert.deepEqual(made, [['a', false]], 'and only once');
    // Undo within the window: nothing is sent, ever.
    offerUndo('Marked as spam · Ann', () => { undone += 1; }, { commit: (k) => made.push(['b', k]) });
    advance(5000);
    toastRoot().button.fire('click');
    advance(60000);
    assert.equal(undone, 1);
    assert.deepEqual(made, [['a', false]]);
    // A newer offer replaces an older one: the older deferred commit is made now.
    offerUndo('Deleted · C', () => {}, { commit: (k) => made.push(['c', k]) });
    offerUndo('Archived · D', () => {});
    assert.deepEqual(made.slice(1), [['c', false]]);
  } finally {
    realTime();
  }
});

await check('a page hidden or left commits what waits, at once and with keepalive', async () => {
  fakeTime();
  try {
    const made = [];
    offerUndo('Deleted · Mara', () => {}, { commit: (keepalive) => made.push(['pagehide', keepalive]) });
    for (const f of winListeners.pagehide || []) f({ type: 'pagehide' });
    assert.deepEqual(made, [['pagehide', true]]);
    assert.equal(toastRoot().innerHTML, '', 'the toast is gone with it');
    offerUndo('Marked as spam · Ann', () => {}, { commit: (keepalive) => made.push(['hidden', keepalive]) });
    document.visibilityState = 'hidden';
    for (const f of docListeners.visibilitychange || []) f({ type: 'visibilitychange' });
    document.visibilityState = 'visible';
    assert.deepEqual(made, [['pagehide', true], ['hidden', true]]);
    advance(60000);
    assert.equal(made.length, 2, 'never a second time');
    // Nothing waiting: hiding the page does nothing.
    offerUndo('Archived · D', () => {});
    for (const f of winListeners.pagehide || []) f({ type: 'pagehide' });
    assert.ok(toastRoot().innerHTML, 'an ordinary Undo stays');
  } finally {
    realTime();
  }
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
