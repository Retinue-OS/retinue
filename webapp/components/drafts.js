// What the user has put in and not sent yet, kept per place on this device,
// so that leaving a page never costs it.
//
// Leaving used to be what the pages guarded against: a tab of the navigation
// row, or a Home press of the Android shell, on a page holding a typed
// message was held, with a note at the bottom asking for a second press. On
// a phone the keyboard came up with the note and the row went away under
// it, so the second press could not even be made. Nothing holds anyone now:
// every composer, command bar and editor writes what it holds here as it
// changes, and reads it back when its place opens again — the words are in
// the field they were typed in, waiting. A dictation still being transcribed
// when the page goes is the service worker's to finish (dictation.js); its
// text lands here too.
//
// Text goes to localStorage, under `retinue-draft:<key>`: synchronous, so a
// write made while the page unloads (pagehide) is not lost, and every change
// is written as it happens, so a page the system kills without an unload
// loses nothing either. Attachments are too large for it and go to
// IndexedDB, written when they are picked or dropped — never only at unload,
// where an asynchronous write may not finish. Both are per device and
// browser. A draft nobody came back to within a month is dropped.
//
// A key names a place: `conv:<thread id | new | new:<project>>` (the
// conversation composer), `chat:<chat id>` (a messenger chat's composer),
// `project-cmd:<id>` and `project-edit:<id>` (a project's command bar and
// raw editor).

const PREFIX = 'retinue-draft:';
const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;

function stale(at) {
  return !Number.isFinite(at) || Date.now() - at > MAX_AGE_MS;
}

// The host's own object stored under `key`, or null.
export function loadDraft(key) {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (!raw) return null;
    const rec = JSON.parse(raw);
    if (!rec || typeof rec !== 'object' || stale(rec.at)) {
      localStorage.removeItem(PREFIX + key);
      return null;
    }
    return rec.v && typeof rec.v === 'object' ? rec.v : null;
  } catch (_e) {
    return null;
  }
}

// Store `value` (a plain object) under `key`; null removes the entry — the
// host passes null once there is nothing left to keep.
export function saveDraft(key, value) {
  try {
    if (!value) localStorage.removeItem(PREFIX + key);
    else localStorage.setItem(PREFIX + key, JSON.stringify({ v: value, at: Date.now() }));
  } catch (_e) { /* storage full or refused: the draft lives in the page only */ }
}

// Drafts nobody came back to. Run once per page; a storage that cannot be
// walked (no key()/length) is simply left as it is.
function pruneText() {
  try {
    const dead = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith(PREFIX)) continue;
      try {
        const rec = JSON.parse(localStorage.getItem(k));
        if (!rec || stale(rec.at)) dead.push(k);
      } catch (_e) { dead.push(k); }
    }
    dead.forEach((k) => localStorage.removeItem(k));
  } catch (_e) { /* ignore */ }
}
pruneText();

// ── Attachments (IndexedDB) ──────────────────────────────────────────────────
// One record per key: {files, at}, `files` being the host's own array (plain
// objects, Blobs welcome). Every operation opens, uses and closes the
// database, and they run one after another, so a write never overtakes the
// read or write issued before it.
const DB_NAME = 'retinue-drafts';
const STORE = 'files';
let queue = Promise.resolve();
let pruned = false;

function openDb() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined' || !indexedDB) { reject(new Error('no indexedDB')); return; }
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => { req.result.createObjectStore(STORE); };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

// Run `fn(store)` in one transaction; resolves to the result of the request
// it returns (if any) once the transaction is complete.
function inStore(mode, fn) {
  const run = queue.then(async () => {
    const db = await openDb();
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        let out;
        const req = fn(tx.objectStore(STORE));
        if (req) req.onsuccess = () => { out = req.result; };
        tx.oncomplete = () => resolve(out);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      });
    } finally {
      db.close();
    }
  });
  queue = run.catch(() => {});
  return run;
}

function pruneFiles() {
  if (pruned) return;
  pruned = true;
  inStore('readwrite', (store) => {
    const req = store.openCursor();
    req.onsuccess = () => {
      const cur = req.result;
      if (!cur) return;
      if (!cur.value || stale(cur.value.at)) cur.delete();
      cur.continue();
    };
    return null;
  }).catch(() => {});
}

// The attachments kept under `key` ([] when none, or no IndexedDB).
export async function loadFiles(key) {
  pruneFiles();
  try {
    const rec = await inStore('readonly', (store) => store.get(key));
    return rec && Array.isArray(rec.files) && !stale(rec.at) ? rec.files : [];
  } catch (_e) {
    return [];
  }
}

// Keep `files` under `key`; an empty list removes the record.
export function saveFiles(key, files) {
  const list = Array.isArray(files) ? files : [];
  return inStore('readwrite', (store) => (list.length
    ? store.put({ files: list, at: Date.now() }, key)
    : store.delete(key))).catch(() => {});
}
