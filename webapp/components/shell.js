// The page's side of the Android shell (ara-android): what the web app does
// when it runs inside the shell's WebView rather than a browser tab.
//
// The shell is a specialised browser and home launcher around this web app
// (docs/dashboard.md, "Inside the Android shell"). It announces itself in
// three ways, all optional — in Chrome or on a desktop none of them exist and
// nothing here does anything:
//   - the user agent ends in ` RetinueShell/<n> (Android)`;
//   - `window.RetinueShell` is installed at document start: {platform,
//     version, setVoiceActive(bool), openSettings()}, and from RetinueShell/2
//     the Health Connect members (healthConnectAvailable(), healthSync(),
//     healthSyncStatus(), setHealthSyncInterval(minutes),
//     setHealthAutoSync(bool));
//   - the shell dispatches `retinue-shell` window events, detail.type `wake`
//     (the wake phrase), `home` (a Home press while the shell is in front) or
//     `health-sync` (a Health Connect sync started, done, failed or skipped).
//
// Every member is feature-detected: an older shell lacks the newer ones, and
// a call that throws is treated as a shell without it.
//
// The home (the conversations viewer on index.html) answers both events in
// place and says so with claimShellEvents(). Every other page of the web app
// answers by going there: a wake loads `/?wake=1`, which the home turns into
// the continuous conversation; a home press loads the home. Always — input
// on the page never holds either back. What the user typed is kept on the
// device and is in its field again when they come back (drafts.js), and a
// dictation still being recorded or transcribed is finished by the service
// worker, its text waiting in that same field (dictation.js).
//
// The press used to be held when the page held unsent input, with a note
// popping up at the bottom of the screen and a second press to go anyway. On
// a phone the keyboard came up with that note and the navigation went away
// under it, so the second press could not even be made. Neither comes back:
// input never stands in the way of leaving, and pop-up notes at the bottom
// of the screen are an anti-pattern here.
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

// ── Health Connect (RetinueShell/2) ─────────────────────────────────────────
// The shell reads the phone's Health Connect store and POSTs the changes to
// /api/health-connect/changes itself (docs/dashboard.md, "Inside the Android
// shell"); the page only starts a deliberate sync, shows the last result and
// edits the two auto-sync settings. The bridge returns strings: "true"/"false"
// and a JSON status document.

// [called, value]: called is false when this shell lacks the member or the
// call threw.
function shellInvoke(name, ...args) {
  const s = typeof window !== 'undefined' ? window.RetinueShell : null;
  if (!s || typeof s[name] !== 'function') return [false, undefined];
  try { return [true, s[name](...args)]; } catch (_e) { return [false, undefined]; }
}

export function healthConnectAvailable() {
  const [called, value] = shellInvoke('healthConnectAvailable');
  return called && String(value) === 'true';
}

// Start a deliberate sync (it always runs, whatever the interval); returns at
// once. False when this shell cannot sync at all.
export function healthSync() {
  return shellInvoke('healthSync')[0];
}

// {available, permissions, last_sync_at, last_result, last_error, last_count,
// min_interval_minutes, auto_on_resume}, or null.
export function healthSyncStatus() {
  const [called, raw] = shellInvoke('healthSyncStatus');
  if (!called || raw == null) return null;
  try {
    const st = typeof raw === 'string' ? JSON.parse(raw) : raw;
    return st && typeof st === 'object' ? st : null;
  } catch (_e) { return null; }
}

export function setHealthSyncInterval(minutes) {
  return shellInvoke('setHealthSyncInterval', Math.round(Number(minutes)))[0];
}

export function setHealthAutoSync(on) {
  return shellInvoke('setHealthAutoSync', !!on)[0];
}

// One line for the last sync, shared by Settings and /sync-health: when, and
// how it went. `st` is healthSyncStatus()'s document.
export function describeHealthSync(st) {
  if (!st) return '';
  if (st.permissions === 'none') return 'Health Connect access not granted yet — the phone shell asks on the next update.';
  if (!st.last_sync_at) return 'No update yet.';
  const when = new Date(st.last_sync_at);
  const at = isNaN(when) ? st.last_sync_at : when.toLocaleString([], {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  if (st.last_result === 'error') return `Last update ${at} failed${st.last_error ? `: ${st.last_error}` : '.'}`;
  if (st.last_result === 'skipped') return `Last update ${at} skipped.`;
  const n = Number.isFinite(st.last_count) ? ` — ${st.last_count} change${st.last_count === 1 ? '' : 's'}` : '';
  const partial = st.permissions === 'partial' ? ' (access granted for some data types only)' : '';
  return `Last update ${at}${n}${partial}.`;
}

// What a `health-sync` event says, as a line; '' for one not worth a line.
export function describeHealthSyncEvent(d) {
  if (!d) return '';
  if (d.state === 'started') return 'Updating health data…';
  if (d.state === 'done') {
    const n = Number.isFinite(d.count) ? ` — ${d.count} change${d.count === 1 ? '' : 's'}` : '';
    return `Health data updated${n}.`;
  }
  if (d.state === 'error') return `Health data update failed${d.error ? `: ${d.error}` : '.'}`;
  if (d.state === 'skipped') {
    const why = {
      interval: 'updated a moment ago',
      'no-permission': 'Health Connect access is not granted',
      unavailable: 'Health Connect is not available on this phone',
      'no-backend-support': 'this dashboard does not accept health data',
    }[d.reason];
    return `Health data update skipped${why ? `: ${why}` : ''}.`;
  }
  return '';
}

// Subscribe to the shell's `health-sync` events: cb(detail) with detail.state
// `started`, `done` (count), `error` (error) or `skipped` (reason). Returns
// the unsubscribe function.
export function onHealthSync(cb) {
  if (typeof window === 'undefined') return () => {};
  const listener = (e) => {
    const d = e && e.detail;
    if (d && d.type === 'health-sync') cb(d);
  };
  window.addEventListener('retinue-shell', listener);
  return () => window.removeEventListener('retinue-shell', listener);
}

// A page that handles `wake` and `home` itself (the home does) claims them,
// so the fallback below stays out of its way.
export function claimShellEvents() { claimed = true; }

if (typeof window !== 'undefined') {
  window.addEventListener('retinue-shell', (e) => {
    if (claimed) return;
    const type = e && e.detail && e.detail.type;
    if (type === 'wake') location.assign('/?wake=1');
    else if (type === 'home') location.assign('/');
  });
}
