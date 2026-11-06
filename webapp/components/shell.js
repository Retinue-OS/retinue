// The page's side of the Android shell (ara-android): what the web app does
// when it runs inside the shell's WebView rather than a browser tab.
//
// The shell is a specialised browser and home launcher around this web app
// (docs/dashboard.md, "Inside the Android shell"). It announces itself in
// three ways, all optional — in Chrome or on a desktop none of them exist and
// nothing here does anything:
//   - the user agent ends in ` RetinueShell/<n> (Android)`;
//   - `window.RetinueShell` is installed at document start: {platform,
//     version, setVoiceActive(bool), openSettings(section?)}, from
//     RetinueShell/2 the Health Connect members (healthConnectAvailable(),
//     healthSync(), healthSyncStatus(), setHealthSyncInterval(minutes),
//     setHealthAutoSync(bool)), and from RetinueShell/3 the endpoint members
//     (endpoints(), selectEndpoint(id); openSettings takes a section);
//   - the shell dispatches `retinue-shell` window events, detail.type `wake`
//     (the wake phrase), `home` (a Home press while the shell is in front),
//     `health-sync` (a Health Connect sync started, done, failed or skipped)
//     or `endpoints` (the shell's endpoint list changed); from RetinueShell/5
//     the keyboard members (keyboardStatus(), keyboardType(text, opts),
//     keyboardKey(name), keyboardSecret(opts), keyboardOpen()) and its
//     `keyboard` events.
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

// ── The app's own settings page (RetinueShell/4) ───────────────────────────
// The Android app has a settings page of its own: this app's settings (which
// Retinue it shows and its endpoints, voice timeouts and shelf size for every
// endpoint, wake phrase, launcher bar, updates), then those for the endpoint
// on screen (health data), then this instance's, which it embeds from
// settings-instance.html. Inside such an app the dashboard's gear opens it
// (nav.js) and the dashboard's Settings page keeps only the way there and the
// instance section.
export function hasShellSettingsPage() {
  const s = typeof window !== 'undefined' ? window.RetinueShell : null;
  return !!s && s.settingsPage === true && typeof s.openSettings === 'function';
}

// Open the phone shell's own settings; `section` names a part of them
// ('endpoints': the endpoint list, or straight into the editor when there is
// one endpoint). A shell before RetinueShell/3 ignores the argument.
export function openShellSettings(section) {
  try {
    const s = window.RetinueShell;
    if (s && typeof s.openSettings === 'function') {
      if (section) s.openSettings(String(section)); else s.openSettings();
      return true;
    }
  } catch (_e) { /* ignore */ }
  return false;
}

// ── Endpoints (RetinueShell/3) ──────────────────────────────────────────────
// The Retinue servers the phone knows, as the shell lists them: [{id, label,
// current}], the label being the instance's own name (the manifest's
// short_name, fetched by the shell) with the host appended only to tell two
// equal names apart. No URLs: this page knows its own origin, and the other
// servers are none of its business. Switching and editing are the shell's;
// the page asks and is left when the switch happens.

// True when this shell has the endpoint members at all.
export function hasShellEndpoints() {
  const s = typeof window !== 'undefined' ? window.RetinueShell : null;
  return !!s && typeof s.endpoints === 'function' && typeof s.selectEndpoint === 'function';
}

// [{id, label, current}], [] for a shell without the member.
export function shellEndpoints() {
  const [called, raw] = shellInvoke('endpoints');
  if (!called || raw == null) return [];
  try {
    const list = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if (!Array.isArray(list)) return [];
    return list
      .filter((e) => e && typeof e === 'object' && typeof e.id === 'string' && e.id)
      .map((e) => ({ id: e.id, label: typeof e.label === 'string' && e.label ? e.label : e.id, current: !!e.current }));
  } catch (_e) { return []; }
}

// Ask the shell to show the endpoint `id`; it loads that server's dashboard,
// so this page is left. False when this shell cannot switch.
export function selectShellEndpoint(id) {
  if (typeof id !== 'string' || !id) return false;
  return shellInvoke('selectEndpoint', id)[0];
}

// Subscribe to the shell's `endpoints` events (the list changed while this
// page is open: an endpoint was added, renamed or removed in the shell's
// settings). Returns the unsubscribe function.
export function onShellEndpoints(cb) {
  if (typeof window === 'undefined') return () => {};
  const listener = (e) => {
    const d = e && e.detail;
    if (d && d.type === 'endpoints') cb();
  };
  window.addEventListener('retinue-shell', listener);
  return () => window.removeEventListener('retinue-shell', listener);
}

// ── Health Connect (RetinueShell/2) ─────────────────────────────────────────
// The shell reads the phone's Health Connect store and uploads the changes
// itself, by WebDAV PUT to /inbox/ (docs/dashboard.md, "Inside the Android
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
// min_interval_minutes, auto_on_resume, enabled}, or null. `enabled` is the
// current endpoint's own health switch (RetinueShell/3; absent in older
// shells, which upload for their one server): false means nothing is read or
// sent for this server, not even on request, until it is turned on in the
// shell's endpoint editor.
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

// The shell uploads health data for the current endpoint at all (see
// healthSyncStatus): true for a shell that does not say.
export function healthUploadEnabled(st) {
  return !st || st.enabled !== false;
}

// One line for the last sync, shared by Settings and /sync-health: when, and
// how it went. `st` is healthSyncStatus()'s document.
export function describeHealthSync(st) {
  if (!st) return '';
  if (!healthUploadEnabled(st)) return 'Health data is not uploaded to this Retinue. Turn it on for this endpoint in the phone shell\u2019s settings.';
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
      disabled: 'health data is not uploaded to this Retinue (off for this endpoint in the phone shell)',
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

// ── Keyboard (RetinueShell/5) ──────────────────────────────────────────────
// The shell can be paired with another device — a VR headset, a TV, a
// computer — as a Bluetooth keyboard, and type into it. Ara's replies carry
// what to type as shell actions (scripts/shell-type.py); conversation.js
// hands them to these members (shell-keyboard.js holds the policy). A secret
// never passes through the page: keyboardSecret() opens a native field in
// the shell that the user's password manager fills, and the page hears only
// `secret-typed` or `secret-cancelled`.

// True when this shell has the keyboard members at all.
export function hasKeyboard() {
  const s = typeof window !== 'undefined' ? window.RetinueShell : null;
  return !!s && typeof s.keyboardStatus === 'function' && typeof s.keyboardType === 'function';
}

// {available, enabled, connected, host: {name, address} | null, layout}, or
// null for a shell without the member.
export function keyboardStatus() {
  const [called, raw] = shellInvoke('keyboardStatus');
  if (!called || raw == null) return null;
  try {
    const st = typeof raw === 'string' ? JSON.parse(raw) : raw;
    return st && typeof st === 'object' ? st : null;
  } catch (_e) { return null; }
}

// Type `text` on the connected device (then Enter, if asked). The outcome
// comes as a `keyboard` event carrying `id`. False when the call could not
// be made.
export function keyboardType(text, { id = '', enter = false } = {}) {
  if (typeof text !== 'string' || !text) return false;
  return shellInvoke('keyboardType', text, JSON.stringify({ id: String(id), enter: !!enter }))[0];
}

// Press one named key: enter, tab, backspace, escape, up, down, left, right,
// home, end, delete, space.
export function keyboardKey(name) {
  if (typeof name !== 'string' || !name) return false;
  return shellInvoke('keyboardKey', name)[0];
}

// Ask the user for a secret in the shell's own native field (autofill from
// their password manager), labelled `label`; the shell types it and
// forgets it.
export function keyboardSecret({ id = '', label = '', enter = false } = {}) {
  return shellInvoke('keyboardSecret', JSON.stringify({ id: String(id), label: String(label), enter: !!enter }))[0];
}

// Open the shell's keyboard screen: pairing, the device, live typing.
export function keyboardOpen() {
  return shellInvoke('keyboardOpen')[0];
}

// Subscribe to the shell's `keyboard` events: cb(detail) with detail.state
// `typed` (count), `error` (error), `secret-typed`, `secret-cancelled` or
// `status` (the connection changed), and the action's `id` where there is
// one. Returns the unsubscribe function.
export function onKeyboard(cb) {
  if (typeof window === 'undefined') return () => {};
  const listener = (e) => {
    const d = e && e.detail;
    if (d && d.type === 'keyboard') cb(d);
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
