// Shell actions on Ara's replies: typing on the device the Android shell is
// paired with as a Bluetooth keyboard (docs/dashboard.md, "Shell actions").
//
// Ara attaches actions to a reply with scripts/shell-type.py; the gateway
// stores them on the message as `shell_actions`:
//   {kind: 'keyboard-type',   id, text, enter, sensitive?, purged?, auto, outcome?}
//   {kind: 'keyboard-key',    id, key, auto, outcome?}
//   {kind: 'keyboard-secret', id, label, enter, auto, outcome?}
// This module holds the policy, the running and the wording; conversation.js
// renders the row under the message and calls run(). The shell members are
// in shell.js.
//
// The policy, in one place:
//   - Nothing runs outside the shell; the row only says the app is needed.
//   - Only plain one-line text runs by itself, ONCE: only when the gateway
//     marked it `auto` (shell_actions.runs_by_itself, in a chat the user
//     opened, on a turn they started from the dashboard, a whole line they
//     had seen there, at most one per reply), only when its
//     message arrived live in the thread open and visible on this device
//     (never on opening a thread, a reload, a history read, or coming back to
//     a page that was hidden), only while it is younger than
//     AUTO_RUN_MAX_AGE_MS, and only if this device has not run it before.
//   - Everything else waits for a tap (needsTap): Enter, a line break (typed
//     as Shift+Enter, which submits in a terminal or a one-line field), a tab
//     (moves to a form's button), any single key (Space presses a focused
//     button), a sensitive text, a password-manager field, a text that is
//     only whitespace or starts or ends with it. The gateway never marks
//     those `auto`; the page checks again.
//   - The phone asks once more, in a confirmation it draws itself, before
//     Enter, a line break, a tab or any key (phoneConfirms) — up to 25 s for
//     a text, 12 s for a key. The row says it waits for that rather than
//     "Typing…"; a declined or unanswered confirmation ends as `cancelled`,
//     as the page's own Stop does, so neither is worded as the user's no.
//   - What a tap types is shown before it: the text itself (visibleText,
//     line breaks and tabs marked), and in the label every Enter it presses
//     (enterCount — a line break is one). Only a sensitive text is not:
//     the row says "••••••", with no way to reveal it. It is
//     gone two minutes after its reply — from the gateway, and from this
//     page's copy too (expireSensitive), offline or hidden included — and as
//     soon as it is reported typed. A deleted text cannot be retyped.
//   - Actions run one after another (the app refuses a second one while it
//     types): the next starts on the previous one's final `keyboard` event,
//     or once the shell has not answered in time (watchdogMs; the row then
//     says so rather than waiting on, and a late answer still counts).
//     Stop (cancel) ends the current one — a typing text, or an open password
//     dialog; never a password being typed — and drops the rest.
//   - Every outcome goes back to the gateway (reportOutcome) from here, not
//     from the thread view, which may have closed meanwhile; the gateway
//     keeps it on the action for Ara's later turns.
//   - A password-manager field never runs by itself: the user taps, the shell
//     opens its own native field, the user's password manager fills it. That
//     secret never reaches the page, the server or Ara.
//   - Every automatic run is remembered by id (localStorage on this origin),
//     so this device types nothing twice by itself; Retype is always a
//     deliberate tap. (Only the Ara app's one WebView can type at all.)
//
// Kept free of DOM work at import time, so the suite can run it under Node
// (tests/test_webapp_shell_keyboard.py).

import {
  hasKeyboard, keyboardStatus, keyboardType, keyboardKey, keyboardSecret,
  keyboardCancel, onKeyboard,
} from './shell.js';

export const AUTO_RUN_MAX_AGE_MS = 2 * 60 * 1000;
// shell_actions.SENSITIVE_TTL_SECONDS: how long a sensitive text outlives its reply.
export const SENSITIVE_TTL_MS = 2 * 60 * 1000;
const KINDS = new Set(['keyboard-type', 'keyboard-key', 'keyboard-secret']);
const DONE_KEY = 'retinue.keyboard.done';
const DONE_MAX = 200;

// The valid shell actions a message carries, [] for none.
export function actionsOf(m) {
  const list = m && Array.isArray(m.shell_actions) ? m.shell_actions : [];
  return list.filter((a) => a && typeof a === 'object' && KINDS.has(a.kind)
    && typeof a.id === 'string' && a.id);
}

// ── Which ids this device has run ──────────────────────────────────────────
function storage() {
  try { return typeof localStorage !== 'undefined' ? localStorage : null; } catch (_e) { return null; }
}

function doneList() {
  const ls = storage();
  if (!ls) return [];
  try {
    const v = JSON.parse(ls.getItem(DONE_KEY) || '[]');
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
  } catch (_e) { return []; }
}

export function isDone(id) {
  return doneList().includes(id);
}

// Record `id` as run; false when it already was, so the caller checks and
// records in one step.
export function claim(id) {
  const list = doneList();
  if (list.includes(id)) return false;
  list.push(id);
  const ls = storage();
  if (ls) {
    try { ls.setItem(DONE_KEY, JSON.stringify(list.slice(-DONE_MAX))); } catch (_e) { /* not kept */ }
  }
  return true;
}

// ── The auto-run decision ──────────────────────────────────────────────────
// Whether the action waits for the user's tap whatever the gateway says — the
// page side of shell_actions.runs_by_itself: everything but plain one-line
// text without Enter, and with no whitespace at its edges. (JavaScript's
// trim() takes a few characters more for whitespace than Python's strip(),
// U+FEFF among them; that only makes the page the stricter.)
export function needsTap(a) {
  if (!a || a.kind !== 'keyboard-type') return true;
  return !!a.enter || a.sensitive === true || typeof a.text !== 'string' || /[\r\n\t]/.test(a.text)
    || !a.text.trim() || a.text.trim() !== a.text;
}

// Whether the Ara app asks on the phone before it carries `a` out: before
// anything that can act on the device — Enter, a line break or a tab in a
// text, any key. Plain one-line text it types at once; a password-manager
// field is a dialog of its own.
export function phoneConfirms(a) {
  if (!a) return false;
  if (a.kind === 'keyboard-key') return true;
  return a.kind === 'keyboard-type' && (!!a.enter || (typeof a.text === 'string' && /[\r\n\t]/.test(a.text)));
}

// `live`: the message arrived while the thread was open here (liveArrivals);
// `now`: ms epoch. Pure but for the done list.
export function shouldAutoRun(action, message, { live = false, now = Date.now(), inShell = hasKeyboard() } = {}) {
  if (!inShell || !live || !action || action.auto !== true) return false;
  if (needsTap(action) || isPurged(action, message, now)) return false;
  const ts = Date.parse(message && message.ts);
  if (!Number.isFinite(ts) || now - ts > AUTO_RUN_MAX_AGE_MS || now - ts < -AUTO_RUN_MAX_AGE_MS) return false;
  return !isDone(action.id);
}

// Which messages of a read arrived live: those a view that had already read
// the thread has not seen. `seen` is that view's Set of message keys — null
// before its first read, and after the page was hidden (the user was not
// looking, so nothing that came meanwhile counts as live). Returns the new
// Set and the live messages; the first read only learns.
export function liveArrivals(messages, seen) {
  const first = !(seen instanceof Set);
  const next = first ? new Set() : new Set(seen);
  const live = [];
  for (const m of messages || []) {
    if (!m || typeof m !== 'object') continue;
    const key = `${m.role}|${m.ts}`;
    if (next.has(key)) continue;
    next.add(key);
    if (!first) live.push(m);
  }
  return { seen: next, live };
}

// ── Deleted texts ──────────────────────────────────────────────────────────
function expired(message, now) {
  const ts = Date.parse(message && message.ts);
  return !Number.isFinite(ts) || now - ts >= SENSITIVE_TTL_MS;
}

// A typing action with nothing left to type: the gateway deleted its text
// (a sensitive one, once typed or after two minutes) — or, given its
// `message`, a sensitive one past the two minutes, whether or not this page
// has heard so from the gateway yet.
export function isPurged(a, message = null, now = Date.now()) {
  if (!a || a.kind !== 'keyboard-type') return false;
  if (a.purged === true || typeof a.text !== 'string' || !a.text) return true;
  return isSensitive(a) && message !== null && expired(message, now);
}

// Drop, from this page's copy of message `m`, the text of every sensitive
// action past the two minutes, as the gateway drops it from its own. True
// when one went.
export function expireSensitive(m, now = Date.now()) {
  let changed = false;
  for (const a of actionsOf(m)) {
    if (isSensitive(a) && typeof a.text === 'string' && expired(m, now)) {
      a.text = null;
      a.purged = true;
      changed = true;
    }
  }
  return changed;
}

export function isSensitive(a) {
  return !!a && a.kind === 'keyboard-type' && a.sensitive === true;
}

// ── Running: one at a time, every outcome reported ─────────────────────────
// The outcome states the gateway keeps (shell_actions.OUTCOME_STATES).
const FINAL_STATES = new Set(['typed', 'error', 'secret-typed', 'secret-cancelled']);

export function isFinal(d) {
  return !!d && FINAL_STATES.has(d.state);
}

const queue = [];          // entries waiting: {action, threadId, message, onState}
let current = null;        // the entry the shell is working on
const started = new Map(); // id -> entry, until its final event (reported then)
const STARTED_MAX = 50;
const reportedListeners = new Set();
let reporterOff = null;

// How long the shell may take before the next action starts anyway (an old
// shell, a lost event): a text at the app's pace with room to spare, a key,
// a password dialog the user is filling.
function watchdogMs(a) {
  // Up to six reports a character (a dead key with Shift), 12 ms each, and more.
  if (a.kind === 'keyboard-type') return 30000 + 100 * (a.text || '').length;
  if (a.kind === 'keyboard-secret') return 10 * 60 * 1000;
  return 15000;
}

function notify(entry, state, extra = {}) {
  if (typeof entry.onState === 'function') {
    try { entry.onState({ state, ...extra }); } catch (_e) { /* the view went */ }
  }
}

function ensureReporter() {
  if (reporterOff) return;
  reporterOff = onKeyboard((d) => {
    if (!d || !d.id || !isFinal(d)) return;
    const entry = started.get(d.id);
    if (entry) {
      started.delete(d.id);
      report(entry, d);
    }
    if (current && current.action.id === d.id) advance();
  });
}

function report(entry, d) {
  reportOutcome(entry.threadId, entry.action, d).then((ok) => {
    for (const cb of reportedListeners) {
      try { cb({ id: entry.action.id, ok, state: d.state, action: entry.action, threadId: entry.threadId }); } catch (_e) { /* next */ }
    }
  });
}

function callShell(a) {
  if (a.kind === 'keyboard-type') return keyboardType(a.text, { id: a.id, enter: !!a.enter });
  if (a.kind === 'keyboard-key') return keyboardKey(a.key, { id: a.id });
  if (a.kind === 'keyboard-secret') return keyboardSecret({ id: a.id, label: a.label, enter: !!a.enter });
  return false;
}

// The watchdog gave up on `entry`: the row is told so ('unanswered', a local
// state the gateway never hears of, since nobody knows whether the device
// took it), so it does not wait on with a Stop that has nothing to stop. The
// entry stays in `started`: a final event that comes late still reaches the
// row and the gateway.
function giveUp(entry) {
  if (current !== entry) return;
  notify(entry, 'unanswered');
  advance();
}

function start(entry) {
  if (isPurged(entry.action, entry.message)) {
    notify(entry, 'deleted');
    return false;
  }
  if (!callShell(entry.action)) {
    notify(entry, 'unavailable');
    return false;
  }
  current = entry;
  entry.timer = setTimeout(() => giveUp(entry), watchdogMs(entry.action));
  started.set(entry.action.id, entry);
  while (started.size > STARTED_MAX) started.delete(started.keys().next().value);
  // `confirm`: the phone asks first, so the row says it waits for that.
  notify(entry, 'pending', phoneConfirms(entry.action) ? { confirm: true } : {});
  return true;
}

function advance() {
  if (current && current.timer) clearTimeout(current.timer);
  current = null;
  while (queue.length && !start(queue.shift())) { /* the next one */ }
}

// Hand one action to the shell, or queue it behind the one running. `ctx`:
// the thread it belongs to (for the outcome report), its message (for the
// two-minute check) and `onState`, told 'queued' / 'pending' / 'unavailable'
// / 'deleted' / 'unanswered' as they happen. False when it cannot run at all.
export function run(action, { threadId = '', message = null, onState = null, now = Date.now() } = {}) {
  if (!action || !KINDS.has(action.kind) || isPurged(action, message, now)) return false;
  if ((current && current.action.id === action.id) || queue.some((e) => e.action.id === action.id)) return true;
  ensureReporter();
  const entry = { action, threadId, message, onState };
  if (current) {
    queue.push(entry);
    notify(entry, 'queued');
    return true;
  }
  return start(entry);
}

// Stop: the shell ends what it is typing (its outcome comes as `error`
// `cancelled`), and whatever waited is dropped, reported as cancelled too.
export function cancel() {
  const waiting = queue.splice(0, queue.length);
  for (const entry of waiting) {
    const d = { state: 'error', error: 'cancelled' };
    notify(entry, 'error', { error: 'cancelled' });
    report(entry, d);
  }
  if (!current) return false;
  // A password dialog open on the phone is closed by the shell, which answers
  // `secret-cancelled`; one that does not answer is given up on soon, not after
  // the dialog's ten minutes. (A secret being typed is not the page's to stop:
  // only the phone's own Stop can, so the page learns nothing from cutting it.)
  if (current.action.kind === 'keyboard-secret') {
    const entry = current;
    clearTimeout(entry.timer);
    entry.timer = setTimeout(() => giveUp(entry), 5000);
  }
  return keyboardCancel();
}

// Whether something is typing or waiting to.
export function isRunning() {
  return !!current || queue.length > 0;
}

// Whether the action `id` is the one the shell works on or waits its turn —
// what a row's "Typing…" and Stop stand for. A row that says so of an action
// that is neither (its final event never reached a view that was away) is
// stale, and the view settles it on Stop.
export function isActive(id) {
  return (!!current && current.action.id === id) || queue.some((e) => e.action.id === id);
}

// cb({id, ok, state, action, threadId}) once an outcome reached the gateway
// (ok) or failed to. Returns the unsubscribe function.
export function onOutcomeReported(cb) {
  reportedListeners.add(cb);
  return () => reportedListeners.delete(cb);
}

// For the test suite: forget the queue and the reporter.
export function _reset() {
  if (current && current.timer) clearTimeout(current.timer);
  current = null;
  queue.length = 0;
  started.clear();
  if (reporterOff) reporterOff();
  reporterOff = null;
}

// Tell the gateway what became of an action — a `keyboard` event `d` with a
// final state. It keeps the outcome for Ara's later turns, and deletes a
// sensitive text reported typed from the stored reply now rather than at the
// two-minute mark. Best effort: the deadline holds regardless.
export async function reportOutcome(threadId, action, d, fetchImpl = (typeof fetch === 'function' ? fetch : null)) {
  if (!action || !threadId || !fetchImpl || !isFinal(d)) return false;
  const body = { state: d.state };
  if (d.state === 'error' && d.error) body.error = String(d.error);
  try {
    const res = await fetchImpl(`/conversations/${encodeURIComponent(threadId)}/shell-actions/${encodeURIComponent(action.id)}/outcome`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    return !!(res && res.ok);
  } catch (_e) { return false; }
}

// The outcome the gateway keeps on an action ({state, error?}), as a local
// outcome for the row, or null.
export function storedOutcome(a) {
  const o = a && a.outcome;
  return o && typeof o === 'object' && FINAL_STATES.has(o.state)
    ? { state: o.state, error: typeof o.error === 'string' ? o.error : undefined } : null;
}

// ── Wording ────────────────────────────────────────────────────────────────
export function hostName(st = keyboardStatus()) {
  const h = st && st.host;
  return h && typeof h.name === 'string' && h.name.trim() ? h.name.trim() : '';
}

// How many times typing `a` presses Enter: each line break in its text
// (typed as Shift+Enter, which submits in a terminal or a one-line field)
// and the Enter after it.
export function enterCount(a) {
  if (!a) return 0;
  if (a.kind === 'keyboard-key') return a.key === 'enter' ? 1 : 0;
  const breaks = a.kind === 'keyboard-type' && typeof a.text === 'string'
    ? (a.text.match(/\r\n|\r|\n/g) || []).length : 0;
  return breaks + (a.enter ? 1 : 0);
}

function enterSuffix(a) {
  const n = enterCount(a);
  return n === 0 ? '' : n === 1 ? ' + Enter' : ` + Enter ×${n}`;
}

// The text a tap types, as the row shows it before the tap: line breaks
// marked ⏎ (each one presses Enter), tabs ⇥. Null when there is none to show
// — a sensitive text, a deleted one, a key, a secret.
export function visibleText(a) {
  if (!a || a.kind !== 'keyboard-type' || isSensitive(a) || isPurged(a)) return null;
  return a.text.replace(/\r\n|\r|\n/g, '⏎\n').replace(/\t/g, '⇥');
}

// What the action will do, as a short label.
export function describeAction(a) {
  if (!a) return '';
  if (a.kind === 'keyboard-type') {
    if (isSensitive(a)) return `Type ••••••${enterSuffix(a)}`;
    if (isPurged(a)) return 'Text deleted';
    const n = (a.text || '').length;
    return `Type ${n} character${n === 1 ? '' : 's'}${enterSuffix(a)}`;
  }
  if (a.kind === 'keyboard-key') return `Press ${keyName(a.key)}`;
  if (a.kind === 'keyboard-secret') return `Password from manager: ${a.label || 'secret'}`;
  return '';
}

export function keyName(k) {
  const names = { enter: 'Enter', tab: 'Tab', backspace: 'Backspace', escape: 'Esc', up: '↑', down: '↓',
    left: '←', right: '→', home: 'Home', end: 'End', delete: 'Delete', space: 'Space' };
  return names[k] || String(k || '');
}

const ERRORS = {
  // The app's switch for this server ("may type on other devices"), or the
  // keyboard itself: the wire tells them apart no more than this.
  'not-enabled': 'typing is off in the Ara app (for this server, or the keyboard itself)',
  'not-connected': 'no device connected',
  'not-in-front': 'the Ara app was not in front',
  'too-long': 'the text is too long',
  'unsupported-char': 'it contains a character the device’s keyboard layout lacks',
  busy: 'still typing the previous text',
  // The page's Stop, and a confirmation on the phone declined or not answered
  // in time: the wire tells them apart no more than this.
  cancelled: 'stopped, or not confirmed on the phone',
  failed: 'the device did not take it',
};

// The outcome line for a `keyboard` event `d` (or a local state), '' for none.
// Second value: where in the Ara app the user can fix it — 'keyboard' (its
// keyboard screen, to connect the device), 'settings' (its settings page,
// which holds this server's switch and the way to the keyboard screen: the
// app refuses keyboardOpen() itself while this server may not type), or ''.
export function describeOutcome(d, host = '') {
  if (!d) return ['', ''];
  const on = host ? ` on ${host}` : '';
  if (d.state === 'queued') return ['Waiting for the one before…', ''];
  if (d.state === 'pending') return [d.confirm ? 'Waiting for confirmation on the phone…' : `Typing${on}…`, ''];
  if (d.state === 'typed') return [`Typed${on} ✓`, ''];
  if (d.state === 'deleted') return ['Text deleted', ''];
  if (d.state === 'secret-typed') return [`Password typed${on} ✓`, ''];
  if (d.state === 'secret-cancelled') return ['Password not typed: cancelled', ''];
  if (d.state === 'error') {
    const why = ERRORS[d.error] || (d.error ? String(d.error) : 'it failed');
    const fix = d.error === 'not-connected' ? 'keyboard' : d.error === 'not-enabled' ? 'settings' : '';
    return [`Not typed: ${why}`, fix];
  }
  if (d.state === 'unavailable') return ['Not typed: this Ara app cannot type on another device', ''];
  // The watchdog's: the app never said what became of it. It may have typed,
  // so the line does not say it did not.
  if (d.state === 'unanswered') return ['No answer from the Ara app — check the device before you retype', ''];
  return ['', ''];
}
