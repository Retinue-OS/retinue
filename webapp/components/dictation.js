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
//   transcribe(key, blob, url, {send})  a dictation the user sent (➤, or a
//                               take of the continuous loop ending on a
//                               pause): `send` is the messages URL of the
//                               thread it was recorded in. Resolves to
//                               {text, sent, unsent} instead: should the page
//                               be gone (or frozen) when the transcript is
//                               in, the worker sends it into that thread
//                               itself (sw.js, "Sent dictations") — `sent`
//                               says it did; `unsent` ('failed' | 'maybe')
//                               that it tried and it did not, or may not,
//                               have gone. Neither: the page sends it.
//                               `call` ({number, name}), with `send`: the
//                               call this take cancelled (conversation.js,
//                               CANCELLED_CALLS), which the worker's send
//                               carries as `cancelled_call`, as the page's
//                               own would, while the take is fresh (sw.js,
//                               DICT_CALL_FRESH_MS).
//   handOff(key, blob, url)     the page is going (pagehide) with the mic
//                               live: the audio so far is sent off as it is,
//                               and nothing waits here for the answer.
//   adopt(key, onJob, {outcome}) the place is on screen: every job left under
//                               it by a page that is gone is this one's now.
//                               onJob(promise) is called once per job, with a
//                               promise of its text as for transcribe(). Jobs
//                               found later (a page restored from the
//                               back-forward cache, a worker restarted under
//                               a running job) arrive the same way. With
//                               `outcome`, the promise resolves to {text,
//                               sent, unsent, send, at, call?} as for a sent
//                               transcribe(); `send` with neither of the
//                               others is a sent dictation whose page went
//                               after it had the text but before it could
//                               send it — the adopter's to send, with the
//                               cancelled `call` it was recorded with, if any.
//
// A promise rejects with Error('elsewhere') when the job is gone from the
// worker without this page having taken it: the page was frozen (the
// back-forward cache) and another page showing the place took the text into
// the draft on the device meanwhile. Not a failure — the host re-reads that
// draft instead of showing an error.
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
// A recording handed off by the page just left may reach the worker after
// the next page's first ask for its place: that page asks once more. An ask
// the worker did not answer (a cold start on a slow phone) is made again.
const ADOPT_RECHECK_MS = 2000;
const ADOPT_RETRY_MS = 5000;

const WAITING = new Map();   // job id -> {key, resolve, reject, timer, acked}
// Jobs settled here: a resync answered before the worker heard they were
// taken lists them still, and must not hand them out a second time.
const SETTLED = new Set();
const ADOPTERS = new Map();  // key -> {onJob, outcome}
let resyncTimer = null;
let listening = false;
let awaitingController = false;

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

// The call a sent take cancelled, as the worker keeps it: {number, name}
// strings, or null. The gateway checks the number itself
// (_cancelled_call_context); this only keeps the shape.
function callOf(call) {
  if (!call || typeof call !== 'object' || typeof call.number !== 'string' || !call.number) return null;
  return { number: call.number.slice(0, 32), name: typeof call.name === 'string' ? call.name.slice(0, 80) : '' };
}

// Settle the job `id` this page waits on, and tell the worker it was taken —
// a task later, once the host's continuation (which writes the text into the
// draft, synchronously) has run.
function settle(id, text, error, sent, unsent, send, at, call) {
  const w = drop(id);
  if (!w) return;
  SETTLED.add(id);
  const t = String(text || '').trim();
  if (error) w.reject(new Error(String(error)));
  else if (w.outcome) {
    const c = callOf(call);
    w.resolve({ text: t, sent: !!sent, unsent: String(unsent || ''), send: !!send, at: Number(at) || 0,
      ...(c ? { call: c } : {}) });
  }
  else w.resolve(t);
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

function wait(id, key, outcome = false) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      const w = drop(id);
      if (w) w.reject(new Error('timeout'));
    }, GIVE_UP_MS);
    WAITING.set(id, { key, resolve, reject, timer, ackTimer: null, acked: false, outcome });
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
    if (d.type === 'dictation-done') settle(d.id, d.text, d.error, d.sent, d.unsent, d.send, d.at, d.call);
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
// result and posting it) is made good the same way. Resolves to null when
// there was no answer (no worker, or none within the deadline) — which says
// nothing about the jobs.
function ask(keys) {
  const c = sw();
  if (!c) return Promise.resolve(null);
  if (!keys.length) return Promise.resolve([]);
  return new Promise((resolve) => {
    const ch = new MessageChannel();
    const timer = setTimeout(() => resolve(null), 5000);
    ch.port1.onmessage = (e) => {
      clearTimeout(timer);
      resolve(Array.isArray(e.data && e.data.jobs) ? e.data.jobs : null);
    };
    try {
      c.controller.postMessage({ type: 'dictation-adopt', keys }, [ch.port2]);
    } catch (_e) {
      clearTimeout(timer);
      resolve(null);
    }
  });
}

async function resync() {
  const keys = new Set(ADOPTERS.keys());
  for (const w of WAITING.values()) keys.add(w.key);
  const jobs = await ask([...keys]);
  if (!jobs) return;
  for (const job of jobs) take(job);
  // A job this page waits on that the worker no longer lists under its place
  // is gone: taken by another page while this one was frozen, or expired. Only
  // one the worker is known to have had (acknowledged, or handed over by it) —
  // one just posted may not be stored yet, and has the ACK timer.
  const listed = new Set(jobs.map((j) => j.id));
  for (const [id, w] of [...WAITING]) {
    if (!w.acked || !keys.has(w.key) || listed.has(id)) continue;
    const gone = drop(id);
    if (gone) { SETTLED.add(id); gone.reject(new Error('elsewhere')); }
  }
}

// A job the worker says this page owns: one already waited on settles when it
// is done; a new one goes to its place's adopter.
function take(job) {
  if (!job || !job.id || SETTLED.has(job.id)) return;
  if (!WAITING.has(job.id)) {
    const adopter = ADOPTERS.get(job.key);
    if (!adopter) return;
    const onJob = adopter.onJob;
    const p = wait(job.id, job.key, adopter.outcome);
    WAITING.get(job.id).acked = true;  // the worker has it: it said so
    try { onJob(p); } catch (_e) { /* the host's problem; the job still settles */ }
  }
  if (job.state === 'done') settle(job.id, job.text, job.error, job.sent, job.unsent, job.send, job.at, job.call);
}

export function transcribe(key, blob, url, { send = '', call = null } = {}) {
  // The page's own fetch has no worker to send for it: the page sends.
  const own = () => (send
    ? fetchText(blob, url).then((text) => ({ text, sent: false, unsent: '', send: true, at: Date.now() }))
    : fetchText(blob, url));
  if (!sw()) return own();
  listen();
  const id = uid();
  const p = wait(id, key, !!send);
  const msg = { type: 'dictation', id, key, url, blob };
  if (send) msg.send = send;
  if (send && callOf(call)) msg.call = callOf(call);
  if (!post(msg)) {
    drop(id);
    return own();
  }
  // Not taken up in time: the page does it itself, and the worker is told to
  // forget the job should it get to it after all — one transcript, not two
  // (a resync listing it meanwhile must not hand it out either: settled).
  WAITING.get(id).ackTimer = setTimeout(() => {
    const w = WAITING.get(id);
    if (!w || w.acked) return;
    drop(id);
    SETTLED.add(id);
    post({ type: 'dictation-taken', id });
    own().then(w.resolve, w.reject);
  }, ACK_MS);
  return p;
}

// No controller yet (a hard reload bypasses the worker; the very first
// visit): the places registered so far are asked for once one takes over.
function whenControlled() {
  if (awaitingController) return;
  try {
    const c = navigator.serviceWorker;
    if (!c) return;
    awaitingController = true;
    c.addEventListener('controllerchange', () => {
      awaitingController = false;
      if (sw()) { listen(); resync(); }
    }, { once: true });
  } catch (_e) { /* no worker here */ }
}

export function handOff(key, blob, url) {
  if (!blob || !blob.size) return false;
  return post({ type: 'dictation', id: uid(), key, url, blob, orphan: true });
}

export function adopt(key, onJob, { outcome = false } = {}) {
  if (!key || typeof onJob !== 'function') return;
  const adopter = { onJob, outcome: !!outcome };
  ADOPTERS.set(key, adopter);
  if (!sw()) { whenControlled(); return; }
  listen();
  const mine = () => ADOPTERS.get(key) === adopter && sw();
  const look = (retries) => ask([key]).then((jobs) => {
    if (jobs) jobs.forEach(take);
    else if (retries > 0) setTimeout(() => { if (mine()) look(retries - 1); }, ADOPT_RETRY_MS);
  });
  look(2);
  setTimeout(() => { if (mine()) look(0); }, ADOPT_RECHECK_MS);
}
