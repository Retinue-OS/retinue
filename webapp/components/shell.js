// The page's side of the Android shell (ara-android): what the web app does
// when it runs inside the shell's WebView rather than a browser tab.
//
// The shell is a specialised browser and home launcher around this web app
// (docs/dashboard.md, "Inside the Android shell"). It announces itself in
// three ways, all optional — in Chrome or on a desktop none of them exist and
// nothing here does anything:
//   - the user agent ends in ` RetinueShell/<n> (Android)`;
//   - `window.RetinueShell` is installed at document start: {platform,
//     version, setVoiceActive(bool), openSettings()};
//   - the shell dispatches `retinue-shell` window events, detail.type `wake`
//     (the wake phrase) or `home` (a Home press while the shell is in front).
//
// The home (the conversations viewer on index.html) answers both events in
// place and says so with claimShellEvents(). Every other page of the web app
// answers by going there: a wake loads `/?wake=1`, which the home turns into
// the continuous conversation; a home press loads the home — unless the page
// holds input that leaving would lose (see registerUnsavedCheck): then it
// stays where it is, and the user finishes or drops that input first.
//
// Pages that do not load this module — the gateway's own (/sends,
// /claude-auth) and anything else outside the web app — never answer either
// event. The shell handles those itself: when the page in front is not one
// of the dashboard's, it loads the home (with `?wake=1` for a wake) rather
// than dispatching an event nobody hears (docs/dashboard.md).

let claimed = false;

export function inShell() {
  return typeof window !== 'undefined' && !!window.RetinueShell;
}

// A voice conversation is engaged (or no longer): the shell keeps the screen
// on and pauses its wake phrase meanwhile. The web app asserts it before it
// asks for the mic (the wake detector must let go of it first), and holds it
// for as long as a continuous conversation is going — Ara's thinking and
// reading time included, or the screen would time out mid-conversation and
// the loop die with it (conversation.js, syncShellVoice). A shell without
// the call, or one that throws, is no reason to fail the recording.
export function setVoiceActive(on) {
  try {
    const s = window.RetinueShell;
    if (s && typeof s.setVoiceActive === 'function') s.setVoiceActive(!!on);
  } catch (_e) { /* ignore */ }
}

export function openShellSettings() {
  try {
    const s = window.RetinueShell;
    if (s && typeof s.openSettings === 'function') { s.openSettings(); return true; }
  } catch (_e) { /* ignore */ }
  return false;
}

// A page that handles `wake` and `home` itself (the home does) claims them,
// so the fallback below stays out of its way.
export function claimShellEvents() { claimed = true; }

// What a page registers to say "leaving now would lose something": a
// function returning true while it holds unsent or unsaved input — a typed
// draft not yet stored, a dictation recording or being transcribed, an
// edit not yet saved. Returns the function that unregisters it. Kept here,
// with no imports, so any page module can use it without a cycle (update.js
// asks the same question of the conversations card before a reload).
// `reveal`, optional: called when that input has just held a wake or a Home
// press back, to show the user where it is (focus the field holding it,
// scroll it into view) — a press that did nothing visible reads as broken.
const unsavedChecks = new Map();

export function registerUnsavedCheck(fn, reveal) {
  if (typeof fn !== 'function') return () => {};
  const entry = { fn, reveal: typeof reveal === 'function' ? reveal : null };
  // Keyed by an object of its own: the same function registered twice is
  // two registrations, each undone by its own unregister.
  const token = {};
  unsavedChecks.set(token, entry);
  return () => { unsavedChecks.delete(token); };
}

// The registration holding unsaved input, or null.
function unsavedHolder() {
  for (const entry of unsavedChecks.values()) {
    try { if (entry.fn()) return entry; } catch (_e) { /* a broken check is no veto */ }
  }
  return null;
}

export function hasUnsavedInput() {
  return !!unsavedHolder();
}

// A short note at the bottom of the page: why the press did nothing. A Home
// press repeated while it shows goes after all (see shellEventHeld).
const NOTE_MS = 3500;
let noteTimer = null;
let homeHeldAt = 0;
function holdNote(type) {
  if (typeof document === 'undefined' || !document.body) return;
  let el = document.getElementById('retinue-shell-note');
  if (!el) {
    el = document.createElement('div');
    el.id = 'retinue-shell-note';
    el.setAttribute('role', 'status');
    el.style.cssText = 'position:fixed;left:16px;right:16px;bottom:calc(16px + env(safe-area-inset-bottom, 0px));' +
      'z-index:2147483647;margin:0 auto;max-width:420px;padding:10px 14px;border-radius:12px;' +
      'background:var(--card-2, #1c2230);color:var(--fg, #e7ebf2);font:14px/1.35 system-ui, sans-serif;' +
      'box-shadow:0 4px 18px rgba(0,0,0,.35);text-align:center;pointer-events:none';
    document.body.appendChild(el);
  }
  el.textContent = type === 'wake'
    ? 'Unsent input here — send or clear it, then wake again.'
    : 'Unsent input here — send or clear it, or press again to leave anyway.';
  el.hidden = false;
  if (noteTimer) clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { el.hidden = true; noteTimer = null; }, NOTE_MS);
}

function hideNote() {
  if (noteTimer) clearTimeout(noteTimer);
  noteTimer = null;
  const el = typeof document !== 'undefined' && document.getElementById('retinue-shell-note');
  if (el) el.hidden = true;
}

// Unsent input here: a Home press does nothing, and a wake goes nowhere —
// the wake phrase can be said again once the input is dealt with, the input
// could not be had back. Said so, and the input shown. True when the event
// is held. The page that claims the events (the home) asks this too before
// acting on one, so its own draft is as safe as any other page's.
//
// The hold stops a slip; it never keeps anyone on a page. A Home press (or
// a press on a tab of the navigation row, which asks as one) made again
// while the note still shows is let through: input that could be neither
// sent nor cleared — a send that never came back, its composer locked —
// once held every way off the page.
export function shellEventHeld(type) {
  const holder = unsavedHolder();
  if (!holder) return false;
  if (type === 'home') {
    const again = homeHeldAt && Date.now() - homeHeldAt < NOTE_MS;
    homeHeldAt = again ? 0 : Date.now();
    if (again) { hideNote(); return false; }
  }
  holdNote(type);
  if (holder.reveal) { try { holder.reveal(); } catch (_e) { /* the note still says it */ } }
  return true;
}

if (typeof window !== 'undefined') {
  window.addEventListener('retinue-shell', (e) => {
    if (claimed) return;
    const type = e && e.detail && e.detail.type;
    if (type !== 'wake' && type !== 'home') return;
    if (shellEventHeld(type)) return;
    if (type === 'wake') location.assign('/?wake=1');
    else location.assign('/');
  });
}
