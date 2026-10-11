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
const unsavedChecks = new Set();

export function registerUnsavedCheck(fn) {
  if (typeof fn !== 'function') return () => {};
  unsavedChecks.add(fn);
  return () => { unsavedChecks.delete(fn); };
}

export function hasUnsavedInput() {
  for (const fn of unsavedChecks) {
    try { if (fn()) return true; } catch (_e) { /* a broken check is no veto */ }
  }
  return false;
}

if (typeof window !== 'undefined') {
  window.addEventListener('retinue-shell', (e) => {
    if (claimed) return;
    const type = e && e.detail && e.detail.type;
    if (type !== 'wake' && type !== 'home') return;
    // Unsent input here: a Home press does nothing, and a wake goes nowhere
    // — the wake phrase can be said again once the input is dealt with,
    // the input could not be had back.
    if (hasUnsavedInput()) return;
    if (type === 'wake') location.assign('/?wake=1');
    else location.assign('/');
  });
}
