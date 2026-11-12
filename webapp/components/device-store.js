// Per-device preferences of the dashboard: the voice timeouts, the shelf size
// and the layout (column sizes, list or card view per card). In a browser they
// live in localStorage, per browser and site, as they always have. Inside the
// Android app (RetinueShell/4 and later) the app holds them, so they apply to
// every Retinue the phone shows instead of to each one separately: a quiet
// desk or a car, and the size of the screen, belong to the device, not to the
// server (docs/dashboard.md, "Inside the Android shell").
//
// Values are strings, exactly as localStorage keeps them, under the same keys;
// the modules that own them parse and clamp what they read either way. The
// app keeps only the keys listed here (DEVICE_KEYS, DEVICE_PREFIXES; its
// DeviceWebSettings mirrors them) and refuses anything else, so a page cannot
// use the store to hand other data to the next Retinue.
//
// A value this browser held before the app took over is handed to the app the
// first time it is read and the app holds none: the first endpoint to read it
// sets it for all of them. The local copy is left as it was, for an older app.
//
// Kept free of DOM work at import time, so the suite can run it under Node
// (tests/test_webapp_device_store.py).

export const DEVICE_KEYS = [
  'retinue-voice-silence-ms',
  'retinue-voice-nospeech-ms',
  'retinue.shelf.size',
  'retinue.layout.v1',
  'retinue.chatpage.v1',
];
export const DEVICE_PREFIXES = ['retinue.view.'];

// What the app accepts, mirrored from its DeviceWebSettings.
export const MAX_KEY_LENGTH = 80;
export const MAX_VALUE_LENGTH = 4096;
const KEY_CHARS = /^[A-Za-z0-9._:-]+$/;

export function isDeviceKey(key) {
  if (typeof key !== 'string' || !key || key.length > MAX_KEY_LENGTH || !KEY_CHARS.test(key)) return false;
  return DEVICE_KEYS.includes(key)
    || DEVICE_PREFIXES.some((p) => key.startsWith(p) && key.length > p.length);
}

// The app's members, when this page runs inside an app that holds the values.
function app() {
  const s = typeof window !== 'undefined' ? window.RetinueShell : null;
  if (!s || typeof s.deviceSettings !== 'function' || typeof s.setDeviceSetting !== 'function') return null;
  return s;
}

// True when the Android app holds these preferences for every endpoint.
export function appHoldsDeviceSettings() {
  return !!app();
}

function appValues(s) {
  try {
    const raw = s.deviceSettings();
    const values = typeof raw === 'string' ? JSON.parse(raw) : raw;
    return values && typeof values === 'object' && !Array.isArray(values) ? values : {};
  } catch (_e) { return {}; }
}

function local() {
  try { return typeof localStorage !== 'undefined' ? localStorage : null; } catch (_e) { return null; }
}

function localGet(key) {
  const ls = local();
  if (!ls) return null;
  try { return ls.getItem(key); } catch (_e) { return null; }
}

function localSet(key, value) {
  const ls = local();
  if (!ls) return;
  try {
    if (value == null) ls.removeItem(key); else ls.setItem(key, value);
  } catch (_e) { /* private mode, quota: not kept */ }
}

// The value of `key`, or null when none is kept.
export function deviceGet(key) {
  const s = app();
  if (!s || !isDeviceKey(key)) return localGet(key);
  const values = appValues(s);
  if (Object.prototype.hasOwnProperty.call(values, key)) {
    const v = values[key];
    return v == null ? null : String(v);
  }
  // Not held by the app yet: this browser's own value, handed over once.
  const own = localGet(key);
  if (own != null && own.length <= MAX_VALUE_LENGTH) {
    try { s.setDeviceSetting(key, own); } catch (_e) { /* the app refused: it stays local */ }
  }
  return own;
}

// Keep `value` (a string; null removes it) under `key`.
export function deviceSet(key, value) {
  const v = value == null ? null : String(value);
  const s = app();
  if (s && isDeviceKey(key) && (v == null || v.length <= MAX_VALUE_LENGTH)) {
    try { s.setDeviceSetting(key, v); return; } catch (_e) { /* fall back to this browser */ }
  }
  localSet(key, v);
}

// cb(keys) whenever kept values change elsewhere: in the app, the `device-settings`
// event (the app's settings page or another page changed them; `keys` lists
// which, or is empty when the app did not say); in a browser, another tab's
// `storage` event, unless `tabs` is false (for a module that watches those
// itself). Returns the unsubscribe function.
export function onDeviceChange(cb, { tabs = true } = {}) {
  if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return () => {};
  const fromApp = (e) => {
    const d = e && e.detail;
    if (!d || d.type !== 'device-settings') return;
    cb(Array.isArray(d.keys) ? d.keys.filter((k) => typeof k === 'string') : []);
  };
  const fromTab = (e) => {
    if (!e || e.key === null) { cb([]); return; }
    if (isDeviceKey(e.key)) cb([e.key]);
  };
  window.addEventListener('retinue-shell', fromApp);
  if (tabs) window.addEventListener('storage', fromTab);
  return () => {
    window.removeEventListener('retinue-shell', fromApp);
    if (tabs) window.removeEventListener('storage', fromTab);
  };
}
