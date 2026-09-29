// The shelf's state: which pages are minimized, and what each one is waiting
// on (issue #282). No DOM here — <retinue-shelf> (shelf.js) draws it, and the
// pages that can be minimized (a thread, a chat, a project) feed it through
// the small API at the end of this file.
//
// One shelf per device and browser, shared by all its windows and tabs: the
// state lives in localStorage, every change is a read-modify-write of the
// whole document, and other tabs learn of it through the `storage` event.
// Nothing is synced across devices — like open windows.
//
// The item model. Membership and status are separate properties:
//
//   key        thread:<id> · chat:<id> · project:<uri> — one item per key
//   kind       thread | chat | project
//   origin     explicit (the minimize button) | automatic (left while Ara
//              was working). Explicit wins; nothing downgrades it.
//   pending    Ara's turn is running (a thread, or a chat's companion)
//   turn       that turn's identity (its pending_since) while pending
//   lastAraTs  ts of the newest message on Ara's side
//   seenTs     ts of the newest Ara message the user has actually seen
//   unread     human messages not yet read (chats)
//   araState   derived: working (pending) | ready (lastAraTs > seenTs) | idle
//   location   where the page was: pane, anchor + offset — never content
//   added / lastUsed   for ordering (added) and eviction (lastUsed)
//
// Projects are plain parked-page shortcuts: never an Ara state.
//
// An item is quiet when it shows no marker (idle and no unread). Capacity
// evicts quiet items only, oldest first; an item with a marker is never
// dropped for room, so the shelf may deliberately hold more than its size and
// shrinks back as items go quiet.
//
// `suppressed` maps a key to a pending turn the user said they are no longer
// waiting for (Close on a working item, archiving it): leaving that thread
// again during that same turn does not put it back. The entry lapses when the
// turn does.

export const STATE_KEY = 'retinue.shelf.v1';
export const SIZE_KEY = 'retinue.shelf.size';
export const DEFAULT_SIZE = 5;
export const MAX_SIZE = 50;
const RESTORE_KEY = 'retinue.shelf.restore';
// A restore handed to the next page load is for that load only.
const RESTORE_TTL_MS = 60 * 1000;

const FACTS = ['title', 'href', 'location', 'pending', 'turn', 'lastAraTs', 'unread', 'avatar'];

export function emptyState() { return { items: [], suppressed: {} }; }

function tsNum(ts) {
  const n = Date.parse(ts || '');
  return Number.isNaN(n) ? 0 : n;
}
function newer(a, b) { return tsNum(a) > tsNum(b); }

export function clampSize(n) {
  const v = Math.round(Number(n));
  if (!Number.isFinite(v) || v < 1) return DEFAULT_SIZE;
  return Math.min(v, MAX_SIZE);
}

export function isQuiet(item) {
  return item.araState === 'idle' && !(item.unread > 0);
}

function deriveState(item) {
  if (item.kind === 'project') return 'idle';
  if (item.pending) return 'working';
  return newer(item.lastAraTs, item.seenTs) ? 'ready' : 'idle';
}

function newItem(f, now) {
  return {
    key: f.key, kind: f.kind || String(f.key).split(':')[0], id: f.id || '',
    title: '', href: '', location: null, avatar: null,
    origin: 'automatic', pending: false, turn: '', lastAraTs: '', seenTs: '',
    unread: 0, araState: 'idle', added: now, lastUsed: now,
  };
}

function merge(item, f) {
  for (const k of FACTS) {
    if (f[k] !== undefined) item[k] = f[k];
  }
  if (!item.pending) item.turn = '';
}

function evict(state, size) {
  while (state.items.length > size) {
    const quiet = state.items.filter(isQuiet).sort((a, b) => a.lastUsed - b.lastUsed);
    if (!quiet.length) break;
    state.items = state.items.filter((i) => i !== quiet[0]);
  }
}

// The whole rule set: (state, action, size) → new state. Pure, so the tests
// can drive it without a browser (tests/test_webapp_shelf.py).
export function reduce(state, action, size = DEFAULT_SIZE) {
  const s = {
    items: ((state && state.items) || []).map((i) => ({ ...i })),
    suppressed: { ...((state && state.suppressed) || {}) },
  };
  const now = action.now || Date.now();
  const key = action.key || (action.item && action.item.key);
  const find = () => s.items.find((i) => i.key === key);
  const drop = () => { s.items = s.items.filter((i) => i.key !== key); };

  switch (action.type) {
    // The minimize button: explicit, whatever was there before. What is on
    // screen at that moment counts as seen.
    case 'minimize': {
      const f = action.item;
      let it = find();
      if (!it) {
        it = newItem(f, now);
        it.seenTs = f.lastAraTs || '';
        s.items.push(it);
      }
      merge(it, f);
      it.origin = 'explicit';
      it.lastUsed = now;
      delete s.suppressed[key];
      break;
    }
    // The user left a page while Ara was working on it. Only for a pending
    // turn the user has not waved off, and never for a muted thread or chat.
    case 'left': {
      const f = action.item;
      if (!f.pending || !f.turn || f.muted) break;
      if (s.suppressed[key] === f.turn) break;
      let it = find();
      if (!it) {
        it = newItem(f, now);
        it.seenTs = f.lastAraTs || '';
        s.items.push(it);
      }
      merge(it, f);
      it.lastUsed = now;
      break;
    }
    // Fresh facts about a key, from a page showing it or from the shelf's own
    // reconciliation read. Partial: only the fields present change.
    case 'observe': {
      const o = action.obs || {};
      if ('pending' in o) {
        const sup = s.suppressed[key];
        if (sup && (!o.pending || o.turn !== sup)) delete s.suppressed[key];
      }
      const it = find();
      if (!it) break;
      if (o.muted) { drop(); break; }
      merge(it, o);
      break;
    }
    // The newest Ara message was actually visible. An automatic item has
    // done its job once the turn that created it is over and seen.
    case 'seen': {
      const it = find();
      if (!it) break;
      if (!it.seenTs || newer(action.ts, it.seenTs)) it.seenTs = action.ts;
      if (it.origin === 'automatic' && !it.pending && !newer(it.lastAraTs, it.seenTs)) drop();
      break;
    }
    case 'close': {
      const it = find();
      if (!it) break;
      if (it.pending && it.turn) s.suppressed[key] = it.turn;
      drop();
      break;
    }
    case 'closeQuiet': {
      s.items.forEach((i) => { i.araState = deriveState(i); });
      s.items = s.items.filter((i) => !isQuiet(i));
      break;
    }
    // Archiving says "I am not waiting for this": no automatic item, now or
    // for the turn under way. A parked (explicit) item stays.
    case 'archived': {
      if (!action.archived) break;
      if (action.turn) s.suppressed[key] = action.turn;
      const it = find();
      if (it && it.origin === 'automatic') drop();
      break;
    }
    // Archive + mute: gone, whatever its origin.
    case 'muted': drop(); break;
    case 'touch': {
      const it = find();
      if (it) {
        it.lastUsed = now;
        if (action.location !== undefined) it.location = action.location;
      }
      break;
    }
    case 'resize': break;
    default: return state;
  }
  s.items.forEach((i) => { i.araState = deriveState(i); });
  evict(s, clampSize(size));
  return s;
}

// What a thread document says, in the shelf's terms.
export function threadFacts(t) {
  if (!t) return {};
  const msgs = t.messages || [];
  let lastAraTs = '';
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    if (msgs[i] && msgs[i].role !== 'user') { lastAraTs = msgs[i].ts || ''; break; }
  }
  return {
    pending: !!t.pending,
    turn: t.pending ? String(t.pending_since || 'pending') : '',
    lastAraTs,
    muted: !!t.muted,
  };
}

// ── Persistence and the page-level API ──────────────────────────────────────

function storage() {
  try { return typeof localStorage !== 'undefined' ? localStorage : null; } catch (_e) { return null; }
}

export function readState() {
  const ls = storage();
  if (!ls) return MEMORY.state;
  try {
    const s = JSON.parse(ls.getItem(STATE_KEY) || 'null');
    if (s && Array.isArray(s.items)) return { items: s.items, suppressed: s.suppressed || {} };
  } catch (_e) { /* corrupt: start over */ }
  return emptyState();
}

// Without storage (private mode, a test) the shelf still works for this page.
const MEMORY = { state: emptyState(), size: DEFAULT_SIZE };

function writeState(s) {
  const ls = storage();
  MEMORY.state = s;
  if (!ls) return;
  try { ls.setItem(STATE_KEY, JSON.stringify(s)); } catch (_e) { /* quota / private mode */ }
}

export function shelfSize() {
  const ls = storage();
  if (!ls) return MEMORY.size;
  try {
    const v = ls.getItem(SIZE_KEY);
    return v == null ? DEFAULT_SIZE : clampSize(v);
  } catch (_e) { return DEFAULT_SIZE; }
}

export function setShelfSize(n) {
  const v = clampSize(n);
  MEMORY.size = v;
  const ls = storage();
  if (ls) { try { ls.setItem(SIZE_KEY, String(v)); } catch (_e) { /* ignore */ } }
  dispatch({ type: 'resize' }, true);
  return v;
}

const SUBS = new Set();
function notify() { SUBS.forEach((fn) => { try { fn(); } catch (_e) { /* one bad listener */ } }); }

export function subscribe(fn) {
  SUBS.add(fn);
  return () => SUBS.delete(fn);
}

// Apply an action to the stored state. A change that changes nothing (a poll
// confirming what is known) writes nothing, so other tabs are not woken.
export function dispatch(action, force) {
  const before = readState();
  const after = reduce(before, action, shelfSize());
  if (!force && JSON.stringify(after) === JSON.stringify(before)) return after;
  writeState(after);
  notify();
  return after;
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('storage', (e) => {
    if (!e || e.key === null || e.key === STATE_KEY || e.key === SIZE_KEY) notify();
  });
}

// Keys shown by a page in this tab right now: the shelf marks them as the
// current page and leaves their facts to that page instead of reading them
// itself.
const LIVE = new Map();
export function goLive(key) {
  LIVE.set(key, (LIVE.get(key) || 0) + 1);
  notify();
  let done = false;
  return () => {
    if (done) return;
    done = true;
    const n = (LIVE.get(key) || 1) - 1;
    if (n > 0) LIVE.set(key, n); else LIVE.delete(key);
    notify();
  };
}
export function isLive(key) { return LIVE.has(key); }

// A restore is handed from the tapped shelf item to the page that opens: per
// tab (sessionStorage), once, and only briefly.
export function setRestore(key, location) {
  try { sessionStorage.setItem(RESTORE_KEY, JSON.stringify({ key, location, at: Date.now() })); }
  catch (_e) { /* no restore: the page opens at its default place */ }
}
export function takeRestore(key) {
  try {
    const r = JSON.parse(sessionStorage.getItem(RESTORE_KEY) || 'null');
    if (!r || r.key !== key) return null;
    sessionStorage.removeItem(RESTORE_KEY);
    return Date.now() - (r.at || 0) < RESTORE_TTL_MS ? (r.location || null) : null;
  } catch (_e) { return null; }
}

export const shelf = {
  minimize: (item) => dispatch({ type: 'minimize', item }),
  left: (item) => dispatch({ type: 'left', item }),
  observe: (key, obs) => dispatch({ type: 'observe', key, obs }),
  seen: (key, ts) => (ts ? dispatch({ type: 'seen', key, ts }) : null),
  close: (key) => dispatch({ type: 'close', key }),
  closeQuiet: () => dispatch({ type: 'closeQuiet' }),
  archived: (key, archived, turn) => dispatch({ type: 'archived', key, archived, turn }),
  muted: (key) => dispatch({ type: 'muted', key }),
  touch: (key, location) => dispatch({ type: 'touch', key, location }),
  has: (key) => readState().items.some((i) => i.key === key),
  items: () => readState().items,
};
