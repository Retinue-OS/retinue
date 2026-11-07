// Shell actions on Ara's replies: typing on the device the Android shell is
// paired with as a Bluetooth keyboard (docs/dashboard.md, "Shell actions").
//
// Ara attaches actions to a reply with scripts/shell-type.py; the gateway
// stores them on the message as `shell_actions`:
//   {kind: 'keyboard-type',   id, text, enter, sensitive?, purged?}
//   {kind: 'keyboard-key',    id, key}
//   {kind: 'keyboard-secret', id, label, enter}
// This module holds the policy and the wording; conversation.js renders the
// row under the message and calls run(). The shell members are in shell.js.
//
// The policy, in one place:
//   - Nothing runs outside the shell; the row only says the app is needed.
//   - A typing action (type, key) runs by itself ONCE: only when the gateway
//     marked it `auto` (shell_actions.runs_by_itself), only when its message
//     arrived live in the thread open on this device (never on opening a
//     thread, a reload or a history read), only while it is younger than
//     AUTO_RUN_MAX_AGE_MS, and only if this device has not run it before.
//     Ara writes one only on the user's explicit request, so the arrival is
//     the moment the user is waiting for.
//   - Whatever presses Enter always waits for a tap: `enter`, the Enter key,
//     and a text with a line break (the app types it as Shift+Enter, which
//     submits in a terminal or a one-line field). The gateway never marks
//     those `auto`; the page checks again.
//   - A sensitive text (a password, PIN or code Ara typed on request) is
//     never shown: the row says "••••••", with no way to reveal it.
//   - Every outcome goes back to the gateway (reportOutcome), which keeps it
//     on the action for Ara's later turns; a sensitive text reported typed is
//     deleted from the stored reply at once, and after two minutes anyway. A
//     purged text cannot be retyped ("Text deleted").
//   - A password-manager field never runs by itself: the user taps, the shell
//     opens its own native field, the user's password manager fills it. That
//     secret never reaches the page, the server or Ara.
//   - Every run is remembered by id (localStorage on this origin), so this
//     device types nothing twice by itself; Retype is always a deliberate
//     tap. (Only the Ara app's one WebView can type at all.)
//
// Kept free of DOM work at import time, so the suite can run it under Node
// (tests/test_webapp_shell_keyboard.py).

import {
  hasKeyboard, keyboardStatus, keyboardType, keyboardKey, keyboardSecret,
} from './shell.js';

export const AUTO_RUN_MAX_AGE_MS = 2 * 60 * 1000;
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

// Record `id` as run; false when it already was (a second tab got there
// first), so the caller checks and records in one step.
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
// `live`: the message arrived while the thread was open here (conversation.js
// knows); `now`: ms epoch. Pure but for the done list.
export function shouldAutoRun(action, message, { live = false, now = Date.now(), inShell = hasKeyboard() } = {}) {
  if (!inShell || !live || !action || action.auto !== true) return false;
  if (action.kind !== 'keyboard-type' && action.kind !== 'keyboard-key') return false;
  if (pressesEnter(action) || isPurged(action)) return false;
  const ts = Date.parse(message && message.ts);
  if (!Number.isFinite(ts) || now - ts > AUTO_RUN_MAX_AGE_MS || now - ts < -AUTO_RUN_MAX_AGE_MS) return false;
  return !isDone(action.id);
}

// Whether the action presses Enter: `enter`, the Enter key, or a line break
// in the text (typed as Shift+Enter). Such an action never runs by itself.
export function pressesEnter(a) {
  if (!a) return false;
  if (a.kind === 'keyboard-key') return a.key === 'enter';
  if (a.kind === 'keyboard-type') return !!a.enter || (typeof a.text === 'string' && /[\r\n]/.test(a.text));
  return !!a.enter;
}

// Hand one action to the shell. Returns false when the shell could not take
// the call (the outcome otherwise arrives as a `keyboard` event).
export function run(action) {
  if (!action || isPurged(action)) return false;
  if (action.kind === 'keyboard-type') return keyboardType(action.text, { id: action.id, enter: !!action.enter });
  if (action.kind === 'keyboard-key') return keyboardKey(action.key, { id: action.id });
  if (action.kind === 'keyboard-secret') return keyboardSecret({ id: action.id, label: action.label, enter: !!action.enter });
  return false;
}

// A typing action whose text the gateway has deleted (a sensitive one, once
// typed or after two minutes): nothing left to type.
export function isPurged(a) {
  return !!a && a.kind === 'keyboard-type' && (a.purged === true || typeof a.text !== 'string' || !a.text);
}

export function isSensitive(a) {
  return !!a && a.kind === 'keyboard-type' && a.sensitive === true;
}

// The outcome states the gateway keeps (shell_actions.OUTCOME_STATES).
const FINAL_STATES = new Set(['typed', 'error', 'secret-typed', 'secret-cancelled']);

export function isFinal(d) {
  return !!d && FINAL_STATES.has(d.state);
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

// What the action will do, as a short label.
export function describeAction(a) {
  if (!a) return '';
  if (a.kind === 'keyboard-type') {
    if (isSensitive(a)) return `Type ••••••${a.enter ? ' + Enter' : ''}`;
    if (isPurged(a)) return 'Text deleted';
    const n = (a.text || '').length;
    return `Type ${n} character${n === 1 ? '' : 's'}${a.enter ? ' + Enter' : ''}`;
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
  'not-enabled': 'the keyboard is off in the Ara app',
  'not-connected': 'no device connected',
  'too-long': 'the text is too long',
  'unsupported-char': 'it contains a character the device’s keyboard layout lacks',
  busy: 'still typing the previous text',
  failed: 'the device did not take it',
};

// The outcome line for a `keyboard` event `d` (or a local state), '' for none.
// Second value: whether the user can fix it in the shell's keyboard screen.
export function describeOutcome(d, host = '') {
  if (!d) return ['', false];
  const on = host ? ` on ${host}` : '';
  if (d.state === 'pending') return [`Typing${on}…`, false];
  if (d.state === 'typed') return [`Typed${on} ✓`, false];
  if (d.state === 'deleted') return ['Text deleted', false];
  if (d.state === 'secret-typed') return [`Password typed${on} ✓`, false];
  if (d.state === 'secret-cancelled') return ['Password not typed: cancelled', false];
  if (d.state === 'error') {
    const why = ERRORS[d.error] || (d.error ? String(d.error) : 'it failed');
    const fixable = d.error === 'not-enabled' || d.error === 'not-connected';
    return [`Not typed: ${why}`, fixable];
  }
  if (d.state === 'unavailable') return ['Not typed: this Ara app cannot type on another device', false];
  return ['', false];
}
