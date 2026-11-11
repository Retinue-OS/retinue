/* Retinue dashboard service worker.
 *
 * Strategy:
 *  - Shell assets (HTML/CSS/JS/icons): cache-first, so the dashboard opens
 *    instantly and works with no connectivity.
 *  - Data documents (/data/*.json): network-first with cache fallback, so you
 *    see fresh curated content when online and the last known state offline.
 *
 * Note: the endpoint sits behind HTTP basic auth. The browser attaches the
 * cached credentials automatically to these same-origin GETs, so both install
 * and runtime fetches work once you have authenticated once.
 *
 * Cache versioning: the hash placeholder in the SHELL constant below is
 * substituted by the gateway when it serves /sw.js, with a content hash
 * computed over the whole shell tree. So the cache name -- and therefore this
 * service worker's own bytes -- change automatically whenever ANY shell asset
 * changes, which is exactly what makes the browser fetch a new worker and drop
 * the stale cache. Nobody has to remember to bump a version by hand. If the
 * placeholder is served verbatim (e.g. the file opened directly off disk), it
 * degrades to a fixed but valid cache name.
 */
const SHELL = 'retinue-shell-__SHELL_HASH__';
const DATA = 'retinue-data-v1';
const SHELL_ASSETS = [
  '/',
  '/conversations.html',
  '/projects.html',
  '/project.html',
  '/news.html',
  '/chats.html',
  '/chat.html',
  '/settings.html',
  '/contacts.html',
  '/viewer.html',
  '/sync-health',
  '/styles.css',
  '/layout.js',
  '/viewport.js',
  '/manifest.webmanifest',
  '/components/base.js',
  '/components/attention.js',
  '/components/attention-sheet.js',
  '/components/agenda.js',
  '/components/messages.js',
  '/components/todo.js',
  '/components/briefing.js',
  '/components/markdown.js',
  '/components/voice.js',
  '/components/clipboard.js',
  '/components/drafts.js',
  '/components/dictation.js',
  '/components/conversation.js',
  '/components/conversations.js',
  '/components/projects.js',
  '/components/project-page.js',
  '/components/chats.js',
  '/components/chat-page.js',
  '/components/news.js',
  '/components/speech.js',
  '/components/continuous.js',
  '/components/shell.js',
  '/components/push.js',
  '/components/update.js',
  '/components/nav.js',
  '/components/contacts.js',
  '/components/shelf.js',
  '/components/shelf-store.js',
  '/components/device-store.js',
  '/components/instance-settings.js',
  '/components/viewer-page.js',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Version introspection for the settings page (components/update.js): a page
// posts {type:'get-version'} with a MessagePort and gets the shell hash back —
// the same value that names the cache, i.e. the version actually being served.
self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'get-version' && e.ports && e.ports[0]) {
    e.ports[0].postMessage({ version: SHELL.replace(/^retinue-shell-/, '') });
  }
});

// ── Dictation ────────────────────────────────────────────────────────────────
// A dictation is transcribed here, not in the page that recorded it, so it is
// finished whether or not that page is still open (components/dictation.js
// is the page's side). A job is kept, audio and all, under the place it was
// recorded for — the draft key of components/drafts.js — and its result stays
// until a page showing that place has taken it: the page that recorded it,
// if it is still there, else the next one to open the place. Each job has one
// owner, a live client; a page that goes releases its jobs, and a job whose
// owner is gone is anybody's who asks for its place.
//
// Messages (page → worker):
//   {type:'dictation', id, key, url, blob, orphan?, send?}  start a job;
//       acknowledged with 'dictation-started', answered with 'dictation-done'
//       {id, key, text, error, sent, unsent} to the owner once it is done.
//       `orphan` (a recording handed off as its page unloads) starts it with
//       no owner at all. `send` (a thread's messages URL) marks a dictation
//       the user sent (➤, or a take of the continuous loop ending on a
//       pause): it goes into that thread whether or not its page is still
//       there — see "Sent dictations" below.
//   {type:'dictation-adopt', keys} + a port  take over the jobs under these
//       places whose owner is gone, restart any this worker was stopped in
//       the middle of, and answer on the port with {jobs} — every job under
//       them the asker owns now, with text or error once done.
//   {type:'dictation-taken', id}  the result is in the place's draft: forget it.
//   {type:'dictation-release'}  the asker is going: its jobs have no owner.
const DICT_DB = 'retinue-dictation';
const DICT_STORE = 'jobs';
const DICT_TIMEOUT_MS = 4 * 60 * 1000;
const DICT_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
// The jobs this worker instance is running, by id, with the run's token: a
// job still marked running but absent here was being run by a worker that
// has been stopped since, and is started again.
const DICT_RUNNING = new Map();
const DICT_SEND_TIMEOUT_MS = 30 * 1000;

// ── Sent dictations ──────────────────────────────────────────────────────────
// A dictation sent with ➤ (or a take of the continuous loop ending on a pause)
// is a message to the thread it was recorded in, exactly as if it had been
// typed there. The page that recorded it sends it once the transcript is in —
// if that page is still there to do so. A page that is gone (a tab of the
// navigation row, Home, a reload) cannot, so the worker does: the transcript
// goes to the thread the job was recorded for (`send`, fixed when recording
// started — never whatever thread is on screen now), and nothing lands in a
// draft. The page that recorded it, still alive and still its owner, sends it
// itself as before (it also carries anything typed in the box). The outcome
// travels with the job: `sent` (in the thread; the page showing it just reads
// the thread again) or `unsent` ('failed', or 'maybe' when the request got
// no answer or the worker was stopped in the middle of it) — then the text
// goes into the draft for review, with a note, as a dictation left there
// always did. Only the dashboard's own message endpoint is ever posted to.
function dictSendUrl(url) {
  return typeof url === 'string' && /^\/conversations\/[^/?#]+\/messages$/.test(url) ? url : '';
}

async function dictSend(rec) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), DICT_SEND_TIMEOUT_MS);
  try {
    const res = await fetch(rec.send, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: rec.text }),
      signal: ctl.signal,
    });
    return res.ok ? { sent: true } : { unsent: 'failed' };
  } catch (err) {
    // Aborted on the deadline: the request may have reached the thread.
    return { unsent: err && err.name === 'AbortError' ? 'maybe' : 'failed' };
  } finally {
    clearTimeout(timer);
  }
}
// Every store operation runs after the one before it, so a 'taken' never
// overtakes the 'dictation' it answers.
let dictQueue = Promise.resolve();

function dictOpen() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DICT_DB, 1);
    req.onupgradeneeded = () => { req.result.createObjectStore(DICT_STORE, { keyPath: 'id' }); };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

// Run fn(store) in one transaction; resolves to what the request it returns
// yielded, once the transaction is complete.
function dictTx(mode, fn) {
  const run = dictQueue.then(async () => {
    const db = await dictOpen();
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(DICT_STORE, mode);
        let out;
        const req = fn(tx.objectStore(DICT_STORE));
        if (req) req.onsuccess = () => { out = req.result; };
        tx.oncomplete = () => resolve(out);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      });
    } finally {
      db.close();
    }
  });
  dictQueue = run.catch(() => {});
  return run;
}

const dictAll = () => dictTx('readonly', (s) => s.getAll());
// Read job `id` and write what fn(record or null) returns, in ONE
// transaction — nothing can change the record in between. fn returning null
// writes nothing. Resolves to what was written, or null.
function dictUpdate(id, fn) {
  let written = null;
  return dictTx('readwrite', (s) => {
    const get = s.get(id);
    get.onsuccess = () => {
      const next = fn(get.result || null);
      if (next) { written = next; s.put(next); }
    };
    return null;
  }).then(() => written);
}

async function dictOwnerAlive(owner) {
  if (!owner) return false;
  try { return !!(await self.clients.get(owner)); } catch (_e) { return false; }
}

function dictResult(type, rec) {
  return {
    type, id: rec.id, key: rec.key, state: rec.state, text: rec.text || '', error: rec.error || '',
    sent: !!rec.sent, unsent: rec.unsent || '', send: !!rec.send, at: rec.at || 0,
  };
}

async function dictNotify(rec) {
  if (!rec || !rec.owner) return;
  try {
    const c = await self.clients.get(rec.owner);
    if (c) c.postMessage(dictResult('dictation-done', rec));
  } catch (_e) { /* the owner went: the result waits for the place */ }
}

// Transcribe job `rec` and store the outcome — unless the job was taken
// meanwhile, or another run of it superseded this one. A new job is stored
// first (and `onStored` told, so the page is acknowledged only for a job
// that is safe here); a restart (`again`) goes on from the stored record,
// and is dropped if that record has gone (taken) or finished meanwhile.
async function dictRun(rec, again, onStored) {
  const token = Math.random().toString(36).slice(2);
  DICT_RUNNING.set(rec.id, token);
  const live = await dictUpdate(rec.id, (cur) => {
    if (again ? (!cur || cur.state !== 'running') : !!cur) return null;
    return { ...(cur || rec), run: token };
  }).catch(() => null);
  if (!live) {
    if (DICT_RUNNING.get(rec.id) === token) DICT_RUNNING.delete(rec.id);
    return;
  }
  if (onStored) onStored();
  let text = '';
  let error = '';
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), DICT_TIMEOUT_MS);
  try {
    const res = await fetch(live.url, {
      method: 'POST',
      headers: { 'Content-Type': (live.blob && live.blob.type) || 'application/octet-stream' },
      body: live.blob,
      signal: ctl.signal,
    });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    text = String((data && data.text) || '').trim();
  } catch (err) {
    error = (err && err.message) || 'failed';
  } finally {
    clearTimeout(timer);
  }
  // A sent dictation is the worker's to send unless the page that recorded
  // it is still there, and still owns it (see "Sent dictations"). Whether
  // that page is alive is asked before the write that decides it; should the
  // owner change in between, the write goes by the record it reads — a job
  // that page took back (restored from the back-forward cache) is its own.
  let originAlive = true;
  if (rec.send && text && !error) {
    const before = await dictTx('readonly', (s) => s.get(rec.id)).catch(() => null);
    if (before && before.owner && before.owner === before.origin) originAlive = await dictOwnerAlive(before.owner);
    else if (before) originAlive = false;
  }
  // The audio is spent once there is a transcript; a failed one is kept no
  // longer either — the note asks for the dictation again. The owner is
  // whoever holds the job at this moment (an adopt may have moved it). The
  // run counts as running until the outcome is stored, so no adopt in between
  // starts it over — nor, while the worker sends it, hands its text out.
  let selfSend = false;
  const done = await dictUpdate(rec.id, (cur) => {
    if (!(cur && cur.run === token)) return null;
    const pageSends = !!(cur.owner && cur.owner === cur.origin && originAlive);
    selfSend = !!(cur.send && text && !error && !pageSends);
    return selfSend
      ? { ...cur, phase: 'sending', text, error, blob: null }
      : { ...cur, state: 'done', text, error, blob: null };
  }).catch(() => null);
  if (!done || !selfSend) {
    if (DICT_RUNNING.get(rec.id) === token) DICT_RUNNING.delete(rec.id);
    if (done) await dictNotify(done);
    return;
  }
  const outcome = await dictSend(done);
  const settled = await dictUpdate(rec.id, (cur) => (cur && cur.run === token
    ? { ...cur, state: 'done', phase: '', ...outcome } : null)).catch(() => null);
  if (DICT_RUNNING.get(rec.id) === token) DICT_RUNNING.delete(rec.id);
  if (!settled) return;
  // Sent with nobody to tell: nothing is left to hand out. A page opening the
  // thread reads the message there.
  if (settled.sent && !settled.owner) {
    await dictTx('readwrite', (s) => s.delete(rec.id)).catch(() => {});
    return;
  }
  await dictNotify(settled);
}

async function dictStart(d, source) {
  const origin = (source && source.id) || '';
  const owner = d.orphan ? '' : origin;
  const rec = {
    id: d.id, key: d.key, url: d.url, blob: d.blob, owner, origin: d.orphan ? '' : origin,
    send: dictSendUrl(d.send), state: 'running', at: Date.now(),
  };
  // Acknowledged once stored, not before: a page whose job could not be
  // stored here (storage refused) hears nothing and transcribes it itself.
  await dictRun(rec, false, () => {
    if (!source || d.orphan) return;
    try { source.postMessage({ type: 'dictation-started', id: d.id }); } catch (_e) { /* ignore */ }
  });
}

async function dictAdopt(keys, asker, port) {
  const want = new Set(keys);
  const all = (await dictAll().catch(() => [])) || [];
  const mine = [];
  const restart = [];
  for (const rec of all) {
    if (Date.now() - (rec.at || 0) > DICT_MAX_AGE_MS) {
      await dictTx('readwrite', (s) => s.delete(rec.id)).catch(() => {});
      continue;
    }
    if (!want.has(rec.key)) continue;
    if (rec.owner !== asker && await dictOwnerAlive(rec.owner)) continue;
    // Stopped in the middle of sending it: whether it went is not known, and
    // it is not sent a second time — the text goes to the draft with a note.
    if (rec.state === 'running' && rec.phase === 'sending' && !DICT_RUNNING.has(rec.id)) {
      await dictUpdate(rec.id, (cur) => (cur && cur.phase === 'sending' && !DICT_RUNNING.has(cur.id)
        ? { ...cur, state: 'done', phase: '', unsent: 'maybe' } : null)).catch(() => {});
    }
    // Taken over in one read-and-write: a run finishing in between would
    // otherwise be overwritten with the stale record read before it.
    let taken = null;
    await dictUpdate(rec.id, (cur) => {
      if (!cur) return null;
      taken = { ...cur, owner: asker };
      return cur.owner === asker ? null : taken;
    }).catch(() => { taken = null; });
    if (!taken) continue;
    mine.push(dictResult('job', taken));
    if (taken.state === 'running' && !DICT_RUNNING.has(taken.id) && taken.blob) restart.push(taken);
  }
  if (port) port.postMessage({ jobs: mine });
  await Promise.all(restart.map((rec) => dictRun(rec, true)));
}

async function dictRelease(owner) {
  if (!owner) return;
  const all = (await dictAll().catch(() => [])) || [];
  for (const rec of all) {
    if (rec.owner !== owner) continue;
    await dictUpdate(rec.id, (cur) => (cur && cur.owner === owner ? { ...cur, owner: '' } : null)).catch(() => {});
  }
}

self.addEventListener('message', (e) => {
  const d = e.data;
  if (!d || typeof d.type !== 'string' || !d.type.startsWith('dictation')) return;
  const source = e.source || null;
  const asker = (source && source.id) || '';
  let job = null;
  if (d.type === 'dictation') {
    // Only the dashboard's own transcription endpoint, and only audio.
    if (!d.id || !d.key || typeof d.url !== 'string' || !d.url.startsWith('/conversations/transcribe')) return;
    if (!(d.blob instanceof Blob)) return;
    job = dictStart(d, source);
  } else if (d.type === 'dictation-adopt' && Array.isArray(d.keys)) {
    job = dictAdopt(d.keys.map(String), asker, e.ports && e.ports[0]);
  } else if (d.type === 'dictation-taken' && d.id) {
    DICT_RUNNING.delete(d.id);
    job = dictTx('readwrite', (s) => s.delete(d.id));
  } else if (d.type === 'dictation-release') {
    job = dictRelease(asker);
  }
  if (job) e.waitUntil(job.catch(() => {}));
});

// ── Web Push ─────────────────────────────────────────────────────────────────
// The dashboard's unread badge only exists while the app is open. These two
// handlers are what reach the user when it is not: the gateway pushes when Ara
// opens a thread (or appends to one), and tapping the notification lands the
// user directly in that thread.
self.addEventListener('push', (e) => {
  let payload = {};
  try {
    payload = e.data ? e.data.json() : {};
  } catch (_) {
    payload = { body: e.data ? e.data.text() : '' };
  }
  const title = payload.title || 'Retinue';
  e.waitUntil(self.registration.showNotification(title, {
    body: payload.body || '',
    // Tagging by thread collapses repeated pushes about the same conversation
    // into one notification instead of stacking them up.
    tag: payload.tag || 'retinue',
    renotify: true,
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    data: { url: payload.url || '/' }
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      // Reuse an already-open dashboard window when there is one: navigating it
      // to the thread beats opening a second copy of the app.
      for (const w of wins) {
        if (new URL(w.url).origin === location.origin) {
          return w.focus().then(() => ('navigate' in w ? w.navigate(target) : null));
        }
      }
      return self.clients.openWindow(target);
    })
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;

  // The attention list and its actions are decided at request time — the
  // mode in force, what is held until when — so a cached copy would lie.
  if (url.pathname === '/attention' || url.pathname.startsWith('/attention/')) return;

  // Conversation API is dynamic (live chat with Ara): never serve from cache,
  // just pass through to the network so threads and replies stay current.
  if (url.pathname === '/conversations' || url.pathname.startsWith('/conversations/')) return;

  // The chat API is the live messenger mirror (messages, unread, the shared
  // draft): a cached copy would show a stale thread. The page shells
  // (/chats.html, /chat.html) are separate paths and stay cache-first.
  if (url.pathname === '/chats' || url.pathname.startsWith('/chats/')) return;

  // The projects endpoints are live (SPARQL over the life store, and the
  // editable per-project file at /projects/item); always go to the network so
  // views and the editor never work on a stale cached copy. The page shells
  // (/projects.html, /project.html) are separate paths and stay cache-first.
  if (url.pathname === '/projects' || url.pathname.startsWith('/projects/')) return;

  // The news feed is ranked at request time — a cached copy would show
  // yesterday's order, with lapsed items still in it. The page shell
  // (/news.html) is a separate path and stays cache-first.
  if (url.pathname === '/news' || url.pathname.startsWith('/news/')) return;

  // The address book is live: a contact filed from a chat card a second ago
  // must be on the list. The page shell (/contacts.html) stays cache-first.
  if (url.pathname === '/contacts' || url.pathname.startsWith('/contacts/')) return;

  // Push config carries the server's current VAPID key; a stale cached copy
  // would silently produce subscriptions this server cannot send to.
  if (url.pathname.startsWith('/push/')) return;

  // The letterbox's WebDAV share is files in flight, not the app.
  if (url.pathname === '/inbox' || url.pathname.startsWith('/inbox/')) return;

  // The SPARQL endpoint answers live from the store (its page, served at the
  // same path, needs the store anyway), and the docs it renders are the
  // image's own: both straight from the network.
  if (url.pathname === '/sparql' || url.pathname.startsWith('/sparql/')) return;
  if (url.pathname.startsWith('/docs/')) return;

  // Vendored libraries (YASGUI, a megabyte) are fetched on first use, then
  // kept in the shell cache: the shell hash covers them, so a new version
  // arrives with the next worker.
  if (url.pathname.startsWith('/vendor/')) {
    e.respondWith(caches.open(SHELL).then((c) => c.match(e.request).then((hit) => hit ||
      fetch(e.request).then((res) => {
        if (res.ok) c.put(e.request, res.clone());
        return res;
      }))));
    return;
  }

  if (url.pathname.startsWith('/data/')) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(DATA).then((c) => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // The project and chat pages carry their subject id in the query string,
  // the file viewer its file (?src=…), and the home its deep links (?item=,
  // the digest's ?digest=); match the cached shell regardless so they open
  // offline too — the viewer with its way back, even when the file cannot load.
  if (url.pathname === '/project.html' || url.pathname === '/chat.html' ||
      url.pathname === '/viewer.html' || url.pathname === '/') {
    e.respondWith(caches.match(url.pathname).then((res) => res || fetch(e.request)));
    return;
  }

  e.respondWith(caches.match(e.request).then((res) => res || fetch(e.request)));
});
