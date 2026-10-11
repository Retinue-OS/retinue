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
// place and says so with claimShellEvents(). Every other page answers by
// going there: a wake loads `/?wake=1`, which the home turns into the
// continuous conversation; a home press loads the home.

let claimed = false;

export function inShell() {
  return typeof window !== 'undefined' && !!window.RetinueShell;
}

// The web app's mic is in use (or no longer): the shell pauses its wake-word
// detection meanwhile and keeps the screen on. A shell without the call, or
// one that throws, is no reason to fail the recording.
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

if (typeof window !== 'undefined') {
  window.addEventListener('retinue-shell', (e) => {
    if (claimed) return;
    const type = e && e.detail && e.detail.type;
    if (type === 'wake') location.assign('/?wake=1');
    else if (type === 'home') location.assign('/');
  });
}
