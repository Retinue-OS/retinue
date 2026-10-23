// Dictation that outlives its page.
//
// A dictation's transcript used to come back to the page that recorded it,
// over a fetch of that page's — so the page could not be left until it was
// in: a tab of the navigation row or a Home press was held meanwhile, and a
// back gesture simply lost it. The transcription now runs in the service
// worker (sw.js, "Dictation"), which keeps each job, audio and all, under the
// place it was recorded for — the draft key of drafts.js — until a page
// showing that place takes its text. Leaving with the mic live is the same:
// the audio recorded so far goes to the worker as the page unloads, and its
// transcript waits in the place's draft.
//
// What a host calls:
//   transcribe(key, blob, url)  the ✓/➤ path: resolves to the text ('' when
//                               no speech was heard), rejects when it failed.
//                               Should the page be left meanwhile, the job
//                               goes on, and its text waits for the place.
//   handOff(key, blob, url)     the page is going (pagehide) with the mic
//                               live: the audio so far is sent off as it is,
//                               and nothing waits here for the answer.
//   adopt(key, onJob)           the place is on screen: every job left under
//                               it by a page that is gone is this one's now.
//                               onJob(promise) is called once per job, with a
//                               promise of its text as for transcribe(). Jobs
//                               found later (a page restored from the
//                               back-forward cache, a worker restarted under
//                               a running job) arrive the same way.
//
// A job belongs to one live page at a time (the worker records the client
// that owns it), so two tabs on the same place never both take one
// transcript; a page that goes releases what it owns. Without a controlling
// service worker — the very first load, a browser without one — transcribe()
// is the page's own fetch, as it always was, and handOff() can do nothing.

const RESYNC_MS = 15000;
// The worker gives a job four minutes; past that, plus a margin, a promise
// still open here is given up rather than left hanging.
const GIVE_UP_MS = 5 * 60 * 1000;
// A worker that has not taken a job up by then (an older version, still in
// charge while a new one installs) leaves it to the page.
const ACK_MS = 4000;

const WAITING = new Map();   // job id -> {key, resolve, reject, timer, acked}
// Jobs settled here: a resync answered before the worker heard they were
// taken lists them still, and must not hand them out a second time.
const SETTLED = new Set();
const ADOPTERS = new Map();  // key -> onJob
let resyncTimer = null;
let listening = false;

function sw() {
  try {
    const c = navigator.serviceWorker;
    return c && c.controller ? c : null;
  } catch (_e) {
    return null;
  }
}

function post(msg) {
  const c = sw();
  if (!c) return false;
  try { c.controller.postMessage(msg); return true; } catch (_e) { return false; }
}

function uid() {
  try {
    if (crypto && crypto.randomUUID) return crypto.randomUUID();
  } catch (_e) { /* fall through */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

// The page's own fetch: what transcription was before the worker took it.
async function fetchText(blob, url) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': blob.type || 'application/octet-stream' },
    body: blob,
  });
  if (!res.ok) throw new Error(String(res.status));
  const data = await res.json();
  return ((data && data.text) || '').trim();
}

// Settle the job `id` this page waits on, and tell the worker it was taken —
// a task later, once the host's continuation (which writes the text into the
// draft, synchronously) has run.
function settle(id, text, error) {
  const w = drop(id);
  if (!w) return;
  SETTLED.add(id);
  if (error) w.reject(new Error(String(error)));
  else w.resolve(String(text || '').trim());
  setTimeout(() => post({ type: 'dictation-taken', id }), 0);
}

// Stop waiting on job `id`; returns what waited on it, if anything.
function drop(id) {
  const w = WAITING.get(id);
  if (!w) return null;
  WAITING.delete(id);
  clearTimeout(w.timer);
  clearTimeout(w.ackTimer);
  if (!WAITING.size) stopResync();
  return w;
}

function wait(id, key) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      const w = drop(id);
      if (w) w.reject(new Error('timeout'));
    }, GIVE_UP_MS);
    WAITING.set(id, { key, resolve, reject, timer, ackTimer: null, acked: false });
    startResync();
  });
}

function listen() {
  if (listening) return;
  const c = sw();
  if (!c) return;
  listening = true;
  c.addEventListener('message', (e) => {
    const d = e && e.data;
    if (!d) return;
    if (d.type === 'dictation-done') settle(d.id, d.text, d.error);
    else if (d.type === 'dictation-started' && WAITING.has(d.id)) WAITING.get(d.id).acked = true;
  });
  // Leaving: what this page owns is anybody's who shows the place next. A
  // page restored from the back-forward cache takes back what is still
  // under its places (resync).
  addEventListener('pagehide', () => post({ type: 'dictation-release' }));
  addEventListener('pageshow', (e) => { if (e && e.persisted) resync(); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') resync();
  });
}

function startResync() {
  if (!resyncTimer) resyncTimer = setInterval(resync, RESYNC_MS);
}
function stopResync() {
  if (resyncTimer) clearInterval(resyncTimer);
  resyncTimer = null;
}

// Ask the worker for the jobs under these places: it hands over the ones
// whose page is gone, restarts any it was running when it was stopped, and
// answers with every job of theirs this page now owns — finished ones with
// their text. A message lost on the way (a worker stopped between storing a
// result and posting it) is made good the same way.
function ask(keys) {
  const c = sw();
  if (!c || !keys.length) return Promise.resolve([]);
  return new Promise((resolve) => {
    const ch = new MessageChannel();
    const timer = setTimeout(() => resolve([]), 5000);
    ch.port1.onmessage = (e) => {
      clearTimeout(timer);
      resolve(Array.isArray(e.data && e.data.jobs) ? e.data.jobs : []);
    };
    try {
      c.controller.postMessage({ type: 'dictation-adopt', keys }, [ch.port2]);
    } catch (_e) {
      clearTimeout(timer);
      resolve([]);
    }
  });
}

async function resync() {
  const keys = new Set(ADOPTERS.keys());
  for (const w of WAITING.values()) keys.add(w.key);
  const jobs = await ask([...keys]);
  for (const job of jobs) take(job);
}

// A job the worker says this page owns: one already waited on settles when it
// is done; a new one goes to its place's adopter.
function take(job) {
  if (!job || !job.id || SETTLED.has(job.id)) return;
  if (!WAITING.has(job.id)) {
    const onJob = ADOPTERS.get(job.key);
    if (!onJob) return;
    const p = wait(job.id, job.key);
    try { onJob(p); } catch (_e) { /* the host's problem; the job still settles */ }
  }
  if (job.state === 'done') settle(job.id, job.text, job.error);
}

export function transcribe(key, blob, url) {
  if (!sw()) return fetchText(blob, url);
  listen();
  const id = uid();
  const p = wait(id, key);
  if (!post({ type: 'dictation', id, key, url, blob })) {
    drop(id);
    return fetchText(blob, url);
  }
  // Not taken up in time: the page does it itself, and the worker is told to
  // forget the job should it get to it after all — one transcript, not two.
  WAITING.get(id).ackTimer = setTimeout(() => {
    const w = WAITING.get(id);
    if (!w || w.acked) return;
    drop(id);
    post({ type: 'dictation-taken', id });
    fetchText(blob, url).then(w.resolve, w.reject);
  }, ACK_MS);
  return p;
}

export function handOff(key, blob, url) {
  if (!blob || !blob.size) return false;
  return post({ type: 'dictation', id: uid(), key, url, blob, orphan: true });
}

export function adopt(key, onJob) {
  if (!key || typeof onJob !== 'function') return;
  ADOPTERS.set(key, onJob);
  if (!sw()) return;
  listen();
  ask([key]).then((jobs) => jobs.forEach(take));
}
