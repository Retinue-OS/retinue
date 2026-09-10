// One conversation with Ara, as an element: <retinue-conversation>.
//
// The thread and everything that talks to it are one object — the message
// bubbles (Markdown via the shared renderer, blockquote/code copy buttons,
// click-to-fill chips that land in THIS composer, attachments, the model and
// cost meta), the pending state while Ara answers, the composer with its
// text, file attachments and voice dictation, the model picker, and the
// read-aloud player over Ara's replies. Every surface that shows a
// conversation embeds this element: the conversations card for an open
// thread and the new-thread composer, and (next) the chat page's companion
// pane. What a host adds is only where the element sits and what "back"
// means there — so the two surfaces render identically because they are the
// same code, not because one copies the other.
//
// Attributes:
//   conversation-id   an existing thread: it is read, polled, replied to.
//   for-project       no thread yet: a composer whose first message opens one
//   project-title     linked to that project (the project page's "Discuss").
//   back              show a back button; a tap dispatches `retinue-back`.
//   bar="none"        no top bar (title, model picker, autoplay, archive) —
//                     for a host whose own header carries those.
//   stamp="clock"     stamp messages with the clock time instead of an age,
//                     for a host that sits beside a clock-stamped timeline.
//
// Events (all bubble and cross the shadow boundary):
//   retinue-back      the back button.
//   retinue-created   the first message opened a thread: {id, conversation}.
//                     The element goes on as that thread; the host adjusts its
//                     own state (an address bar entry, say).
//   retinue-sent      a message went out: {id, conversation}.
//   retinue-archived  the thread was archived or restored: {id, archived}.
//   retinue-thread    the thread was (re)read: {id, conversation}.
//   retinue-open      the read-aloud bar asks for another thread: {id} — it
//                     follows the user out of a thread and offers a way back.
//
// State that must outlive one element instance lives at module level, keyed
// by conversation id (or the composer key): drafts and picked files — a user
// who leaves a thread and comes back finds their text — dictation jobs in
// flight, which land their transcript in that draft (and send it, on the
// send path) whether or not the thread is still on screen, and the read-aloud
// reader, which keeps speaking when its thread is left and shows its bar
// (<retinue-read-aloud>, defined here too) wherever a host places one.
//
// The gateway's conversation API this element speaks:
//   GET  /conversations/<id>            the thread with its messages
//   POST /conversations                 open a thread ({message, project?,
//                                       project_title?, model?, attachments?})
//   POST /conversations/<id>/messages   reply ({message, attachments?})
//   POST /conversations/<id>/read       clear the unread badge
//   POST /conversations/<id>/model      pin the thread's model
//   POST /conversations/<id>/archive|unarchive
//   POST /conversations/transcribe      dictation audio → {text, lang}
//   GET  /conversation-models           the offered models (once per page)

import { esc, fmtAge } from './base.js';
import { renderMarkdown, MD_CSS } from './markdown.js';
import { canRecord, recordingRowHtml, statusRowHtml, Waveform, VOICE_CSS } from './voice.js';
import { Reader, speechAvailable } from './speech.js';

const LIST_URL = '/conversations';
const POLL_MS = 4000;
// While Ara is answering the reply is what the user is waiting for.
const PENDING_POLL_MS = 1500;
const PENDING_WARN_SECONDS = 2 * 60;
const PENDING_STALE_SECONDS = 10 * 60;
const TEXTAREA_MAX_HEIGHT_RATIO = 0.35;
// Keep the client cap in step with the gateway's CONVERSATION_MAX_ATTACHMENT_BYTES
// (default 25 MiB) so oversized files are rejected before a doomed upload.
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
// Where the read-aloud player remembers how far it got (see savePosition):
// leaving the page kills the browser's speech, and this is what lets the
// thread offer to carry on from that passage instead of from the top.
const POSITION_KEY = 'retinue-voice-position';
const POSITION_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const AUTOPLAY_KEY = 'retinue-voice-autoplay';
// Types the gateway will serve with `Content-Disposition: inline`, i.e. that the
// browser shows in place instead of saving. Mirrors _INLINE_SAFE_TYPES in
// web-gateway.py — offering "view" for anything else would just download it.
const INLINE_SAFE_TYPES = new Set([
  'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif',
  'application/pdf', 'text/plain',
]);
// The draft key of a composer that has no thread yet. One key for every such
// composer: the text a user typed towards a new thread follows them to the
// next new-thread composer, as it did before.
const NEW_KEY = 'new';

// ── Module state: what outlives one element instance ─────────────────────────
// Drafts and picked files per conversation (or NEW_KEY): {text, files}, and
// for NEW_KEY the model picked for the thread about to open — all of what a
// composer holds before a send, so leaving and returning finds it intact.
const DRAFTS = new Map();
// Dictation jobs in flight per key: {sending, phase}. A job owns that
// conversation's input row until it completes — every other conversation
// keeps its normal row, so text and voice stay usable there meanwhile.
const VOICE_JOBS = new Map();
// Transcription errors per key, surfaced by that conversation's composer —
// a background job's failure must not pop up in whatever is open.
const VOICE_ERRORS = new Map();
// The connected element per key, so a job that outlived its element can
// still refresh the thread if the user is back on it.
const LIVE = new Map();
// Autoplay bookkeeping per thread: replies already voiced or seen, and
// whether the thread's history has been marked so only new replies speak.
const SPOKEN = new Map();
const AUTO_READY = new Map();
// The read-aloud bars on the page (see RetinueReadAloud).
const BARS = new Set();
// The offered model list, fetched once per page; '' means the gateway default.
let MODELS = [];
let modelsPromise = null;
let autoplay = false;
try { autoplay = localStorage.getItem(AUTOPLAY_KEY) === '1'; } catch (_e) { /* ignore */ }

// The read-aloud player: one engine, one message at a time, cut into
// passages (speech.js). It is deliberately not per element — speech is one
// thing per page, and a reading goes on after its thread is left.
const READER = new Reader();
let PLAYING = null;     // {conv, ts, who, title} of the loaded message
let SCRUBBING = false;  // the user is dragging a bar's position slider
READER.onprogress = (ev) => onReaderProgress(ev);
// A backgrounded engine may drop the utterance it was speaking without a
// word; on return the reader checks and picks the passage up again.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') READER.resync();
});

// True while a page reload would lose typed-but-unsent input in any
// conversation — components/update.js consults this before auto-reloading.
export function hasUnsentInput() {
  for (const d of DRAFTS.values()) {
    if ((d.text && d.text.trim()) || (d.files && d.files.length)) return true;
  }
  return false;
}

// The offered model list, fetched once and shared. A failure (or a
// single-model list) simply leaves every picker hidden.
export function loadModels() {
  if (!modelsPromise) {
    modelsPromise = (async () => {
      try {
        const res = await fetch('/conversation-models', { cache: 'no-store' });
        if (!res.ok) return MODELS;
        const data = await res.json();
        if (Array.isArray(data.models)) MODELS = data.models;
      } catch (_err) { /* picker stays hidden */ }
      return MODELS;
    })();
  }
  return modelsPromise;
}

function draftOf(key) {
  let d = DRAFTS.get(key);
  if (!d) { d = { text: '', files: [], model: '' }; DRAFTS.set(key, d); }
  return d;
}

// A chip's prefill or a dictation APPENDS to what is there: it augments the
// draft, it never wipes work in progress.
function appendToDraft(key, text) {
  const d = draftOf(key);
  d.text = d.text ? `${d.text.replace(/\s*$/, '')} ${text}` : text;
}

function fmtSize(n) {
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Cost with enough precision to stay meaningful for cheap turns: sub-cent
// values get more decimals so they don't collapse to "~$0.00".
function fmtCost(v) {
  const c = Math.abs(v);
  if (c === 0) return '0';
  if (c < 0.01) return c.toFixed(4);
  if (c < 1) return c.toFixed(3);
  return c.toFixed(2);
}

function fmtClock(iso) {
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? ''
    : t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// Strip Markdown so the synthesizer reads clean prose: no code fences,
// backticks, emphasis marks, quote markers, list bullets or table rules;
// links and chips read as their labels, a raw URL as its host.
function plainForSpeech(text) {
  return String(text == null ? '' : text)
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\[\[chip:\s*([^|\]]+?)\s*(?:\|[^\]]*)?\]\]/gi, '$1')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\((?:[^)]+)\)/g, '$1')
    .replace(/\bhttps?:\/\/([^\s/)]+)[^\s)]*/g, '$1')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/gm, ' ')
    .replace(/^\s*[-*_]{3,}\s*$/gm, ' ')
    .replace(/^\s*[-+*•]\s+/gm, ' ')
    .replace(/\|/g, ', ')
    .replace(/[*_#]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

async function copyToClipboard(btn, root) {
  const text = btn.getAttribute('data-copy') || '';
  let ok = true;
  try {
    await navigator.clipboard.writeText(text);
  } catch (_err) {
    // Fallback for contexts without the async clipboard API (older WebViews).
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      (root || document.body).appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand('copy');
      ta.remove();
    } catch (_e) {
      ok = false;
    }
  }
  const prev = btn.dataset.label || btn.textContent;
  btn.dataset.label = prev;
  btn.textContent = ok ? 'Copied ✓' : 'Error';
  btn.classList.toggle('done', ok);
  setTimeout(() => {
    if (!btn.isConnected) return;
    btn.textContent = btn.dataset.label || 'Copy';
    btn.classList.remove('done');
  }, 1500);
}

// ── The read-aloud reader's bookkeeping ──────────────────────────────────────

function playingInfo(conv, m) {
  const who = m.agent || (m.role === 'agent' ? 'Retinue' : 'Ara');
  return { conv: conv.id, ts: m.ts, who, title: conv.title || '' };
}

// Is `m` (of the thread with id `convId`) the message in the player?
function isLoaded(convId, m) {
  const p = PLAYING;
  return !!(p && convId && m && READER.loaded && p.conv === convId && p.ts === m.ts);
}

// Load message `m` of thread `conv` into the player and speak it from
// `fraction` (0..1) of its text. Called from a tap where possible: the first
// speak of a page load must sit inside a user gesture on iOS.
function playMessage(conv, m, fraction) {
  if (!conv || !m || !speechAvailable()) return;
  const clean = plainForSpeech(m.text);
  if (!clean) return;
  PLAYING = playingInfo(conv, m);
  READER.load([{ id: m.ts, lang: m.lang, text: clean }], { fraction: fraction || 0 });
  READER.resume();
}

function savedPosition() {
  try {
    const raw = localStorage.getItem(POSITION_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (!p || typeof p !== 'object' || !p.conv || !p.ts) return null;
    if (!(Date.now() - (Number(p.at) || 0) < POSITION_TTL_MS)) return null;
    const fraction = Number(p.fraction);
    if (!(fraction > 0 && fraction < 1)) return null;
    return { conv: String(p.conv), ts: String(p.ts), fraction };
  } catch (_e) { return null; }
}

// Remember where the reading is. A position at the very start is not worth
// keeping (nothing is lost by starting over), and one at the end is done.
function savePosition(ev) {
  const p = PLAYING;
  if (!p) return;
  try {
    if (!(ev.fraction > 0 && ev.fraction < 1)) { localStorage.removeItem(POSITION_KEY); return; }
    localStorage.setItem(POSITION_KEY, JSON.stringify({
      conv: p.conv, ts: p.ts, fraction: ev.fraction, at: Date.now(),
    }));
  } catch (_e) { /* ignore */ }
}

function clearPosition() {
  try { localStorage.removeItem(POSITION_KEY); } catch (_e) { /* ignore */ }
}

// The ✕ on the bar: stop for good, and forget the place.
function closePlayer() {
  READER.stop();
  clearPosition();
}

// Every reader transition lands here (and a tick every quarter second while
// it plays). Bookkeeping first, then every bar and every open thread — in
// place, never a re-render: a full render mid-reading would drop a composer's
// focus and a thread's scroll.
function onReaderProgress(ev) {
  if (ev.event === 'end') clearPosition();
  else if (ev.event === 'load' || ev.event === 'start' || ev.event === 'piece' ||
           ev.event === 'pause' || ev.event === 'seek') savePosition(ev);
  if (ev.event === 'stop') PLAYING = null;
  const tick = ev.event === 'tick';
  BARS.forEach((bar) => bar.sync(tick));
  if (!tick) LIVE.forEach((el) => el._applyReading());
}

function positionLabel(pr, fraction, previewing) {
  const pct = `${Math.round(fraction * 100)}%`;
  if (previewing) return pct;
  if (pr.error) return 'Could not play — tap ▶ to try again';
  if (pr.state === 'paused') return `Paused · ${pct}`;
  if (pr.starting) return 'Starting …';
  // Time left at the measured speaking rate: an estimate, hence the tilde.
  const left = pr.remaining;
  const eta = left >= 90 ? `~${Math.round(left / 60)} min left` : `~${left} s left`;
  return `${pct} · ${eta}`;
}

// ── <retinue-read-aloud>: the player bar ─────────────────────────────────────
// Shows what is being read and where, with pause, seek and passage skips.
// Empty (and invisible) while nothing is loaded, so a host can place it once
// and the bar appears without a re-render. `here` names the conversation the
// bar sits in; a reading from another thread names that thread and makes it a
// way back (a tap dispatches `retinue-open`).
class RetinueReadAloud extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._key = '';
    BARS.add(this);
    this.shadowRoot.innerHTML = `<style>${BAR_CSS}</style><div class="host"></div>`;
    this._wire();
    this.sync(false);
  }

  disconnectedCallback() {
    BARS.delete(this);
  }

  get here() { return this.getAttribute('here') || ''; }

  // What the bar's structure depends on; when it changes the bar is rebuilt,
  // otherwise only its slider and label move.
  _barKey() {
    const p = PLAYING;
    if (!READER.loaded || !p) return 'idle';
    return [READER.state, p.conv, p.ts, READER.error || '', p.conv === this.here ? 'here' : 'away'].join('|');
  }

  _html() {
    const p = PLAYING;
    if (!READER.loaded || !p) return '';
    const playing = READER.speaking;
    // Read from another view (a list, or a different thread): name the thread
    // too, and make it a way back to the message.
    const elsewhere = p.conv !== this.here;
    const who = esc(p.who) + (elsewhere && p.title ? ` · ${esc(p.title)}` : '');
    const whoEl = elsewhere
      ? `<button type="button" class="p-who p-link" data-p-open="${esc(p.conv)}" ` +
        `title="Open this conversation">${who}</button>`
      : `<span class="p-who">${who}</span>`;
    const main = playing ? 'Pause' : 'Play';
    return `<div class="player" role="group" aria-label="Read aloud">` +
      `<button type="button" class="p-btn" data-p-back title="Back a passage" ` +
      `aria-label="Back a passage">⏮</button>` +
      `<button type="button" class="p-btn p-main" data-p-toggle title="${main}" aria-label="${main}">` +
      `${playing ? '⏸' : '▶'}</button>` +
      `<button type="button" class="p-btn" data-p-fwd title="Forward a passage" ` +
      `aria-label="Forward a passage">⏭</button>` +
      `<div class="p-track">` +
      `<input type="range" class="p-seek" min="0" max="1000" step="1" value="0" ` +
      `aria-label="Position in the message" data-p-seek>` +
      `<div class="p-info">${whoEl}<span class="p-pos" data-p-pos></span></div></div>` +
      `<button type="button" class="p-btn p-close" data-p-close title="Stop reading" ` +
      `aria-label="Stop reading">✕</button></div>`;
  }

  // Listeners live on the host box, which the in-place rebuilds keep.
  _wire() {
    const host = this.shadowRoot.querySelector('.host');
    host.addEventListener('click', (e) => {
      if (e.target.closest('[data-p-toggle]')) { READER.toggle(); return; }
      if (e.target.closest('[data-p-back]')) { READER.back(); return; }
      if (e.target.closest('[data-p-fwd]')) { READER.forward(); return; }
      if (e.target.closest('[data-p-close]')) { closePlayer(); return; }
      const open = e.target.closest('[data-p-open]');
      if (open) {
        this.dispatchEvent(new CustomEvent('retinue-open', {
          bubbles: true, composed: true, detail: { id: open.getAttribute('data-p-open') } }));
      }
    });
    // Dragging previews the position; releasing seeks there. While the drag
    // lasts the reader's ticks leave the bar alone.
    host.addEventListener('input', (e) => {
      if (!e.target.matches('[data-p-seek]')) return;
      SCRUBBING = true;
      this._update(Number(e.target.value) / 1000);
    });
    host.addEventListener('change', (e) => {
      if (!e.target.matches('[data-p-seek]')) return;
      SCRUBBING = false;
      READER.seek(Number(e.target.value) / 1000);
    });
    // A release that lands on the value the drag started from fires no change
    // event; the bar must not stay frozen on the preview then. The check runs
    // a task later, after any change event of the same release.
    const unscrub = (e) => {
      if (!e.target.matches('[data-p-seek]')) return;
      setTimeout(() => {
        if (!SCRUBBING) return;
        SCRUBBING = false;
        this._update();
      }, 0);
    };
    host.addEventListener('pointerup', unscrub);
    host.addEventListener('pointercancel', unscrub);
  }

  // Bring the bar in line with the reader: rebuilt when its structure
  // changed, otherwise (and on every tick) only the slider and label move.
  sync(tickOnly) {
    const host = this.shadowRoot && this.shadowRoot.querySelector('.host');
    if (!host) return;
    const key = this._barKey();
    if (!tickOnly && key !== this._key) {
      this._key = key;
      host.innerHTML = this._html();
    }
    this._update();
  }

  // Slider and label only. `preview` (0..1) is the value under the user's
  // finger while dragging; otherwise the reader's own position is shown —
  // except during a drag, when only the drag's own previews may touch the bar.
  _update(preview) {
    const previewing = typeof preview === 'number';
    if (SCRUBBING && !previewing) return;
    const bar = this.shadowRoot && this.shadowRoot.querySelector('.player');
    if (!bar) return;
    const pr = READER.progress();
    const fraction = previewing ? preview : pr.fraction;
    const seek = bar.querySelector('[data-p-seek]');
    if (seek) {
      seek.value = String(Math.round(fraction * 1000));
      seek.style.setProperty('--p', `${(fraction * 100).toFixed(1)}%`);
      seek.setAttribute('aria-valuetext', `${Math.round(fraction * 100)}%`);
    }
    const pos = bar.querySelector('[data-p-pos]');
    if (pos) {
      const text = positionLabel(pr, fraction, previewing);
      if (pos.textContent !== text) pos.textContent = text;
    }
  }
}

// ── <retinue-conversation> ───────────────────────────────────────────────────

class RetinueConversation extends HTMLElement {
  static get observedAttributes() { return ['conversation-id']; }

  constructor() {
    super();
    this._id = '';           // the thread; '' while this is a new-thread composer
    this._thread = null;     // the thread document as last read
    this._busy = false;      // a send or an archive is in flight
    this._attachError = '';  // last attach error (e.g. file too big)
    this._focusNext = false; // focus the input after the next render
    this._hadFocus = false;  // the input had focus before the current re-render
    this._threadSig = '';
    this._pollTimer = null;
    this._adopting = false;  // the id is being set from within (a created thread)
    // Voice: record a message (server transcribes) — the recorder is one per
    // element, its job (see VOICE_JOBS) belongs to the conversation.
    this._recState = 'idle'; // idle | recording
    this._recChunks = [];
    this._mediaRecorder = null;
    this._recStream = null;
    this._recIntent = null;  // what to do with the transcript: 'review' | 'send'
    this._recAborted = false;
    this._wave = new Waveform(this);
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._id = this.getAttribute('conversation-id') || '';
    LIVE.set(this._key(), this);
    loadModels().then(() => this._syncPicker());
    this._focusNext = true;
    this.render();
    if (this._id) {
      this._load().then(() => this.render());
      this._schedulePoll();
    }
  }

  disconnectedCallback() {
    // Leaving with the mic live is the same as tapping the green check: the
    // recording stops, and its transcript lands in this conversation's draft,
    // waiting there, reviewed on return.
    this._finishRecording('review');
    this._stopPolling();
    if (LIVE.get(this._key()) === this) LIVE.delete(this._key());
    this._wave.stop();
    this._stopStream();
    // The reader is not stopped: a reading follows the user out of the thread.
  }

  attributeChangedCallback(name, was, now) {
    if (name !== 'conversation-id' || was === now || this._adopting || !this.isConnected) return;
    // Pointed at another thread: forget this one and read that one.
    if (LIVE.get(this._key()) === this) LIVE.delete(this._key());
    this._stopPolling();
    this._id = now || '';
    this._thread = null;
    this._threadSig = '';
    this._attachError = '';
    LIVE.set(this._key(), this);
    this._focusNext = true;
    this.render();
    if (this._id) {
      this._load().then(() => this.render());
      this._schedulePoll();
    }
  }

  get conversationId() { return this._id; }
  get thread() { return this._thread; }
  get dirty() {
    const d = draftOf(this._key());
    return !!((d.text && d.text.trim()) || d.files.length);
  }

  _key() { return this._id || NEW_KEY; }
  // The model picked for the thread about to open. Module state like the
  // draft it belongs to: the element is torn down whenever the host leaves
  // the composer, and a choice made before the first message must survive
  // that as the text does.
  get _newModel() { return draftOf(NEW_KEY).model || ''; }
  set _newModel(v) { draftOf(NEW_KEY).model = v || ''; }
  _emit(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { bubbles: true, composed: true, detail: detail || {} }));
  }

  // What a first message carries: the project this composer is about.
  _seed() {
    return {
      project: this.getAttribute('for-project') || '',
      project_title: this.getAttribute('project-title') || '',
    };
  }

  // ── Reading the thread ─────────────────────────────────────────────────────
  async _load() {
    if (!this._id) return;
    try {
      const res = await fetch(`/conversations/${encodeURIComponent(this._id)}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const t = await res.json();
      if (!t || (t.id && t.id !== this._id)) return;
      this._thread = t;
      if (t.unread) this._markRead();
      this._maybeAutoplay(t);
      this._restorePosition(t);
      this._emit('retinue-thread', { id: this._id, conversation: t });
    } catch (_err) {
      // Offline or gateway down: keep the last rendered state.
    }
  }

  async _markRead() {
    try { await fetch(`/conversations/${encodeURIComponent(this._id)}/read`, { method: 'POST' }); }
    catch (_err) { /* the badge is cosmetic; a later read retries */ }
  }

  _schedulePoll() {
    this._stopPolling();
    if (!this._id) return;
    const pending = !!(this._thread && this._thread.pending);
    this._pollTimer = setTimeout(() => this._poll(), pending ? PENDING_POLL_MS : POLL_MS);
  }

  _stopPolling() {
    if (this._pollTimer) clearTimeout(this._pollTimer);
    this._pollTimer = null;
  }

  async _poll() {
    this._pollTimer = null;
    if (!this.isConnected || !this._id) return;
    if (!document.hidden) {
      await this._load();
      // Partial update only: never replace the input form (would cancel the
      // browser dictation session) or the scroll container (would jump).
      this._partialUpdate();
    }
    this._schedulePoll();
  }

  // Apply what a read brought (new messages, the pending state, the title,
  // the model) without rebuilding the shadow DOM: the input stays alive so
  // dictation, IME composition, focus and selection survive, and the thread
  // keeps its scroll position.
  _partialUpdate() {
    const root = this.shadowRoot;
    const t = this._thread;
    if (!root || !t) return;
    // A cold start renders the frame before the thread is read, so the
    // message container may not exist yet — only a full render introduces it.
    const threadEl = root.querySelector('.thread');
    if (!threadEl) { this.render(); return; }
    const titleEl = root.querySelector('[data-title]');
    if (titleEl) {
      const want = t.title || 'Conversation';
      if (titleEl.textContent !== want) titleEl.textContent = want;
    }
    const sig = this._signature(t);
    if (sig !== this._threadSig) {
      // Preserve scroll position. Only auto-stick to bottom when the user was
      // already near the bottom before new content arrived; otherwise a
      // background poll must not fight the user's reading/scrolling.
      const prevBottom = threadEl.scrollHeight - threadEl.scrollTop;
      const stickToBottom = (prevBottom - threadEl.clientHeight) < 40;
      const prevTop = threadEl.scrollTop;
      threadEl.innerHTML = this._messagesHtml(t);
      this._threadSig = sig;
      threadEl.scrollTop = stickToBottom ? threadEl.scrollHeight : Math.max(0, threadEl.scrollHeight - prevBottom);
      if (!stickToBottom) threadEl.scrollTop = Math.max(threadEl.scrollTop, prevTop);
    }
    this._updatePendingStatus(t);
    this._syncPicker();
  }

  _signature(t) {
    return JSON.stringify([
      (t.messages || []).map((m) => [m.role, m.text, m.ts, (m.attachments || []).length,
        m.model_name || '', m.cost_usd ?? '', m.agent || '']),
      !!t.pending,
      t.pending_since || '',
      t.pending_status || '',
      t.pending_error || '',
      t.title || '',
    ]);
  }

  // ── Rendering ──────────────────────────────────────────────────────────────
  render() {
    const root = this.shadowRoot;
    if (!root) return;
    // Remember whether our input had focus so a re-render can restore it (and
    // not steal focus when the user wasn't typing).
    const prev = root.querySelector('[data-form] textarea');
    this._hadFocus = !!(prev && root.activeElement === prev);
    const mode = this._id ? 'thread' : 'new';
    const here = esc(this._id);
    root.innerHTML = `<style>${CSS}${VOICE_CSS}${MD_CSS}</style>` +
      `<div class="conv" data-mode="${mode}">` +
      this._barHtml() +
      (mode === 'thread' ? this._threadHtml() : this._newHtml()) +
      `<retinue-read-aloud here="${here}"></retinue-read-aloud>` +
      this._inputRow(mode === 'thread' ? 'Reply …' : 'Ask Ara something …') +
      `</div>`;
    this._threadSig = this._thread ? this._signature(this._thread) : '';
    this._wire();
    // After a full render of a thread, scroll to bottom so the latest message
    // is visible (matches typical chat-app behaviour on open).
    const threadEl = root.querySelector('.thread');
    if (threadEl) threadEl.scrollTop = threadEl.scrollHeight;
  }

  _barHtml() {
    if (this.getAttribute('bar') === 'none') return '';
    const back = this.hasAttribute('back')
      ? '<button class="back" data-back aria-label="Back">&#8249;</button>' : '';
    if (!this._id) {
      return `<div class="thread-bar">${back}<span class="bar-title">New conversation</span></div>`;
    }
    const t = this._thread;
    if (!t) {
      return `<div class="thread-bar">${back}<span class="bar-title muted" data-title>&#8230;</span></div>`;
    }
    const archiveBtn = t.archived
      ? '<button class="pill" data-unarchive>Unarchive</button>'
      : '<button class="pill" data-archive>Archive</button>';
    const autoBtn = speechAvailable()
      ? `<button class="iconbtn${autoplay ? ' on' : ''}" data-autoplay ` +
        `title="Speak Ara's replies as they arrive" aria-label="Speak replies as they arrive" ` +
        `aria-pressed="${autoplay}">${autoplay ? '\u{1F50A}' : '\u{1F507}'}</button>`
      : '';
    return `<div class="thread-bar">${back}` +
      `<span class="bar-title" data-title>${esc(t.title || 'Conversation')}</span>` +
      `<span class="bar-actions"><span data-picker>${this._modelPickerHtml()}</span>` +
      `${autoBtn}${archiveBtn}</span></div>`;
  }

  _threadHtml() {
    const t = this._thread;
    if (!t) return `<div class="thread"></div>`;
    return `<div class="thread">${this._messagesHtml(t)}</div>`;
  }

  // The composer that has no thread yet: what the first message will be about.
  _newHtml() {
    const seed = this._seed();
    const chip = seed.project
      ? `<div class="about-chip">About: ${esc(seed.project_title || seed.project)}</div>` : '';
    const hint = seed.project
      ? `<p>Ask Ara about this project &mdash; she reads its current state first.</p>`
      : `<p>Ask Ara anything &mdash; she picks it up with full context.</p>`;
    // The picker sits in the body as a labeled, full-width row — cramped into
    // the top bar it truncated its labels and was easy to miss, and picking the
    // model is exactly the choice to make before the first message goes out
    // (it can still be switched later from the thread bar).
    return chip +
      `<div class="empty"><span class="e-ico" aria-hidden="true">&#x1F4AC;</span>` + hint +
      `<span data-picker>${this._modelPickerHtml({ wide: true })}</span></div>`;
  }

  _messagesHtml(t) {
    const canSpeak = speechAvailable();
    const msgs = (t.messages || []).map((m, idx) => {
      const cls = m.role === 'user' ? 'me' : (m.role === 'agent' ? 'agent' : 'ara');
      const reading = isLoaded(t.id, m) ? ' reading' : '';
      // The sender label: the acting agent's own name when a relay set one
      // (e.g. "Coach"), else the role default. "You" / "Retinue" / "Ara".
      const defaultWho = m.role === 'user' ? 'You' : (m.role === 'agent' ? 'Retinue' : 'Ara');
      const who = (m.role !== 'user' && m.agent) ? m.agent : defaultWho;
      const speakBtn = (canSpeak && m.role !== 'user' && (m.text || '').trim())
        ? this._speakBtnHtml(t, m, idx) : '';
      return `<div class="msg ${cls}${reading}"><div class="msg-head">` +
        `<small class="who">${esc(who)}</small>` +
        this._metaHtml(m) +
        speakBtn + `</div>` +
        `<div class="bubble">${this._renderBubble(m.text)}` +
        this._attachmentsHtml(t.id, m.attachments) +
        `</div></div>`;
    }).join('');
    const pending = t.pending
      ? `<div class="msg ara pending-msg"><div class="bubble pending">` +
        `<span data-pending-status>${esc(this._pendingStatusText(t))}</span>` +
        `<small class="pending-help">${esc(this._pendingHelpText(t))}</small>` +
        `</div></div>`
      : '';
    return msgs + pending;
  }

  // The header meta after the sender name: for an answer bubble, the model
  // short-name and the turn's list-price cost (marked "~$" — a fictional
  // pay-per-token estimate, not the subscription's actual bill); for every
  // message, its timestamp — an age, or the clock time where the host asks
  // for it. Each piece is optional — older messages predating this metadata
  // simply omit what they lack. Rendered as middot-separated muted text so it
  // reads as one quiet line.
  _metaHtml(m) {
    const bits = [];
    if (m.model_name) bits.push(`<span class="m-model">${esc(m.model_name)}</span>`);
    if (typeof m.cost_usd === 'number' && isFinite(m.cost_usd)) {
      bits.push(`<span class="m-cost" title="Approximate list-price cost — not the subscription bill">` +
        `~$${fmtCost(m.cost_usd)}</span>`);
    }
    if (m.ts) {
      const stamp = this.getAttribute('stamp') === 'clock' ? fmtClock(m.ts) : fmtAge(m.ts);
      if (stamp) {
        bits.push(`<time class="m-ts" datetime="${esc(m.ts)}" title="${esc(m.ts)}">${esc(stamp)}</time>`);
      }
    }
    if (!bits.length) return '';
    return `<small class="msg-meta">${bits.join('<span class="m-sep">·</span>')}</small>`;
  }

  // Render any files a message carries. Both links hit the gateway's per-thread
  // attachment endpoint; `?inline=1` asks for a Content-Disposition the browser
  // renders in place rather than saving. Viewing is the primary tap: a download
  // writes a fresh copy to storage every time, so re-reading one invoice leaves
  // invoice(1).pdf, invoice(2).pdf behind. Types the gateway refuses to serve
  // inline get the download link alone — an inline href would save anyway.
  _attachmentsHtml(cid, atts) {
    if (!Array.isArray(atts) || !atts.length) return '';
    const items = atts.map((a) => {
      const url = `/conversations/${encodeURIComponent(cid)}/attachments/${encodeURIComponent(a.id)}`;
      const name = a.filename || 'attachment';
      const size = fmtSize(a.size);
      const type = String(a.content_type || '').split(';')[0].trim().toLowerCase();
      const viewable = INLINE_SAFE_TYPES.has(type);
      // Same-tab navigation, deliberately: in a standalone PWA a target="_blank"
      // link is handed to a browsing context outside the app window, with no
      // history behind it — the back gesture then leaves the PWA instead of
      // returning to the thread. Navigating in place keeps the viewer on the
      // dashboard's own history stack.
      const open = viewable
        ? `<a class="attach" href="${esc(url)}?inline=1">`
        : `<a class="attach" href="${esc(url)}" download="${esc(name)}">`;
      return `<div class="attach-row">` + open +
        `<span class="a-icon" aria-hidden="true">\u{1F4CE}</span>` +
        `<span class="a-name">${esc(name)}</span>` +
        (size ? `<span class="a-size">${esc(size)}</span>` : '') +
        `</a>` +
        (viewable
          ? `<a class="a-dl" href="${esc(url)}" download="${esc(name)}" title="Save a copy">↓</a>`
          : '') +
        `</div>`;
    }).join('');
    return `<div class="attachments">${items}</div>`;
  }

  // A message body via the shared Markdown renderer (markdown.js), so bubbles
  // and project pages show the same text the same way. Blockquotes — how Ara
  // offers ready-to-send drafts — and fenced code blocks — ready-to-paste
  // prompts — carry a copy button that puts the clean, un-prefixed text on
  // the clipboard. The delegated click handler on the thread serves both.
  _renderBubble(text) {
    return renderMarkdown(text, {
      quote: (raw, inner) =>
        `<blockquote class="md-quote quote"><div class="q-text">${inner}</div>` +
        `<button class="copy" type="button" data-copy="${esc(raw)}">Copy</button>` +
        `</blockquote>`,
      code: (raw, _lang, inner) =>
        `<div class="code-wrap">${inner}` +
        `<button class="copy code-copy" type="button" data-copy="${esc(raw)}">Copy</button>` +
        `</div>`,
    });
  }

  _speakBtnHtml(t, m, idx) {
    const loaded = isLoaded(t.id, m);
    const playing = loaded && READER.speaking;
    const label = playing ? 'Pause' : (loaded ? 'Resume reading' : 'Read aloud');
    const glyph = playing ? '⏸' : (loaded ? '▶' : '\u{1F50A}');
    return `<button class="speak${loaded ? ' on' : ''}" type="button" data-speak-idx="${idx}" ` +
      `title="${label}" aria-label="${label}">${glyph}</button>`;
  }

  _pendingAgeSeconds(t) {
    const started = t.pending_since || t.updated || t.created || null;
    if (!started) return null;
    const ms = Date.parse(started);
    if (!Number.isFinite(ms)) return null;
    return Math.max(0, Math.floor((Date.now() - ms) / 1000));
  }

  _pendingAgeText(seconds) {
    if (seconds === null) return '';
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }

  _pendingStatusText(t) {
    const age = this._pendingAgeSeconds(t);
    const ageText = this._pendingAgeText(age);
    const prefix = t.pending_status || 'Ara is working on this';
    return ageText ? `${prefix} (${ageText})` : `${prefix} …`;
  }

  _pendingHelpText(t) {
    const age = this._pendingAgeSeconds(t);
    if (age !== null && age >= PENDING_STALE_SECONDS) {
      return 'No progress has been reported for a while. It may still finish, but it is reasonable to stop waiting and try another reply later.';
    }
    if (age !== null && age >= PENDING_WARN_SECONDS) {
      return 'Still waiting for the background Ara session. This can take a few minutes if tools or other sessions are busy.';
    }
    return 'This thread will update automatically when Ara replies.';
  }

  _updatePendingStatus(t) {
    const status = this.shadowRoot.querySelector('[data-pending-status]');
    if (status) status.textContent = this._pendingStatusText(t);
    const help = this.shadowRoot.querySelector('.pending-help');
    if (help) help.textContent = this._pendingHelpText(t);
  }

  // The model dropdown. Governs Ara's own turn only (dispatched subagents keep
  // their own models) — the title says so. Hidden unless the gateway offers
  // more than one model, so a single-model deployment sees no clutter. The
  // thread's choice is its `model`; '' means the gateway default, which the
  // list carries not as its own row but as a `default: true` flag on the
  // concrete entry that default runs on — show that entry as selected. An
  // unpinned thread that Ara junior escalated stays with Ara senior (the
  // gateway keeps it on the frontier tier until the picker is touched), so it
  // shows as a distinct, unpickable "escalated" row rather than as the
  // default: any choice — the default included — is then a change, which is
  // what clears the escalation. Only when the gateway could not name its
  // default does a hidden placeholder keep the select from claiming a model
  // it is not running. `wide` renders the roomy composer form: a visible
  // "Model" caption and an untruncated select, instead of the bar's compact
  // gear + capped-width one.
  _modelPickerHtml({ wide = false } = {}) {
    const models = MODELS || [];
    if (models.length < 2) return '';
    // A thread's model and escalation are unknown until it has been read;
    // showing the default for a thread that may be pinned would be a claim.
    if (this._id && !this._thread) return '';
    let sel = this._id ? (this._thread.model || '') : this._newModel;
    const escalated = !!(this._id && !sel && this._thread.escalated);
    if (!escalated && !models.some((m) => m.id === sel)) {
      const def = models.find((m) => m.default);
      sel = def ? def.id : '';
    }
    const placeholder = escalated
      ? '<option value="" hidden selected>Ara senior (escalated)</option>'
      : (models.some((m) => m.id === sel) ? ''
        : '<option value="" hidden selected>Default</option>');
    const opts = placeholder + models.map((m) =>
      `<option value="${esc(m.id)}"${m.id === sel ? ' selected' : ''}>` +
      `${esc(m.label)}</option>`).join('');
    const title = 'Model for Ara’s replies in this conversation. ' +
      'Dispatched subagents (Coach, Medic, …) keep their own models.';
    const caption = wide
      ? '<span class="mp-label">Model</span>'
      : '<span class="mp-ico" aria-hidden="true">⚙</span>';
    return `<label class="model-pick${wide ? ' wide' : ''}" title="${title}">` +
      caption +
      `<select data-model aria-label="${title}">${opts}</select></label>`;
  }

  // Bring the picker in line with the state — in place, never a full render.
  // Left alone while the user has it open, so a poll cannot snap a half-made
  // choice away.
  _syncPicker() {
    const root = this.shadowRoot;
    const host = root && root.querySelector('[data-picker]');
    if (!host) return;
    const sel = host.querySelector('[data-model]');
    if (sel && root.activeElement === sel) return;
    const html = this._modelPickerHtml({ wide: !this._id });
    if (host.innerHTML === html) return;
    host.innerHTML = html;
    const next = host.querySelector('[data-model]');
    if (next) next.addEventListener('change', () => this._onModelChange(next.value));
  }

  _inputRow(placeholder) {
    const key = this._key();
    const d = draftOf(key);
    const disabled = this._busy ? 'disabled' : '';
    const chips = d.files.map((f, i) =>
      `<span class="chip"><span class="c-name">${esc(f.name)}</span>` +
      `<span class="c-size">${esc(fmtSize(f.size))}</span>` +
      `<button type="button" class="c-x" data-rmfile="${i}" aria-label="Remove attachment" ${disabled}>&times;</button></span>`
    ).join('');
    const chipRow = d.files.length ? `<div class="chips">${chips}</div>` : '';
    const errText = this._attachError || VOICE_ERRORS.get(key) || '';
    const errRow = errText ? `<div class="attach-err">${esc(errText)}</div>` : '';
    // The voice flow owns the input row: a live waveform with its own controls
    // while recording, then a status line while this conversation's dictation
    // is transcribed (and, on the send path, sent). The textarea stays out of
    // the DOM for the entire flow, so the phone keyboard never pops up
    // mid-dictation.
    if (this._recState === 'recording') {
      return `<div class="composer">` + chipRow + errRow + recordingRowHtml() + `</div>`;
    }
    const job = VOICE_JOBS.get(key);
    if (job) {
      const label = job.phase === 'sending' ? 'Sending …'
        : (job.sending ? 'Transcribing & sending …' : 'Transcribing …');
      return `<div class="composer">` + chipRow + errRow + statusRowHtml(label) + `</div>`;
    }
    const micTitle = 'Record a voice message';
    const micDisabled = (this._busy || this._recState !== 'idle') ? 'disabled' : '';
    const micBtn = canRecord()
      ? `<button type="button" class="mic" ` +
        `data-mic title="${micTitle}" aria-label="${micTitle}" ${micDisabled}>\u{1F3A4}</button>`
      : '';
    // A lean row keeps the width for the text field: mic on the left, the
    // attach control tucked inside the field, send on the right.
    return `<div class="composer">` + chipRow + errRow +
      `<form class="row" data-form>` + micBtn +
      `<div class="field">` +
      `<textarea rows="1" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}" autocomplete="off" ${disabled}>` +
      `${esc(d.text)}</textarea>` +
      `<label class="clip" title="Attach a file" aria-label="Attach a file">` +
      `<input type="file" multiple hidden data-file ${disabled}>` +
      `<span aria-hidden="true">\u{1F4CE}</span></label>` +
      `</div>` +
      `<button type="submit" title="Send" aria-label="Send" ${disabled}>➤</button></form></div>`;
  }

  // ── Sending ────────────────────────────────────────────────────────────────
  async _send(text) {
    const key = this._key();
    const d = draftOf(key);
    // A message needs text or at least one attachment.
    if (this._busy || (!text.trim() && !d.files.length)) return;
    this._busy = true;
    try {
      const conv = await sendMessage(this._id, text, d.files, this._seed(), this._newModel);
      d.text = '';
      d.files = [];
      this._attachError = '';
      if (!this._id) {
        this._becomeCreated(conv);
      } else {
        this._thread = conv;
        this._emit('retinue-sent', { id: this._id, conversation: conv });
      }
    } catch (_err) {
      // A soft failure: the draft stays in the input for a retry.
    } finally {
      this._busy = false;
      this._focusNext = true;
      this.render();
      this._schedulePoll();
    }
  }

  // A first message opened a thread: go on as it. The composer's model
  // choice is consumed — this thread carries it now — and the host hears
  // of the thread before the send.
  _becomeCreated(conv) {
    this._newModel = '';
    this._adopt(conv);
    this._emit('retinue-created', { id: this._id, conversation: conv });
    this._emit('retinue-sent', { id: this._id, conversation: conv });
  }

  // Become the thread a first message just opened.
  _adopt(conv) {
    if (LIVE.get(this._key()) === this) LIVE.delete(this._key());
    this._id = String(conv.id);
    this._thread = conv;
    this._adopting = true;
    try { this.setAttribute('conversation-id', this._id); } finally { this._adopting = false; }
    LIVE.set(this._key(), this);
  }

  // Read picked files into base64 (chunked, so large files don't overflow the
  // String.fromCharCode call stack) and stage them as pending attachments.
  async _addFiles(fileList) {
    this._attachError = '';
    const d = draftOf(this._key());
    for (const file of Array.from(fileList || [])) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        this._attachError = `"${file.name}" is too large (max ${fmtSize(MAX_ATTACHMENT_BYTES)}).`;
        continue;
      }
      try {
        const buf = new Uint8Array(await file.arrayBuffer());
        let binary = '';
        for (let i = 0; i < buf.length; i += 0x8000) {
          binary += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        }
        d.files.push({
          name: file.name,
          type: file.type || 'application/octet-stream',
          size: file.size,
          data: btoa(binary),
        });
      } catch (_err) {
        this._attachError = `Couldn't read "${file.name}".`;
      }
    }
    this._focusNext = true; // return focus to the textarea to keep typing
    this.render();
  }

  _removeFile(index) {
    const d = draftOf(this._key());
    d.files.splice(index, 1);
    this._attachError = '';
    this.render();
  }

  // Drop a chip's prefill text into the composer for review. Deliberately does
  // NOT send: the user reads (and can edit) it, then taps Send — same contract
  // as a dictation transcribed for review. Appends to whatever the user has
  // already typed rather than replacing it, then re-renders to show the text
  // and focus the field with the caret at the end.
  _fillComposer(text) {
    appendToDraft(this._key(), text);
    this._focusNext = true;
    this.render();
  }

  // ── Voice input: record → live waveform → transcribe (review or send) ──────
  // Tapping the mic swaps the input row for a recording row: a live waveform
  // (or a simulated one where the Web Audio API is unavailable) with three
  // controls — abort on the left (discard the recording), and on the right a
  // green check (transcribe, then drop the text into the composer for review)
  // and a send button (transcribe and send in one go, with no detour through
  // the textarea, so the phone keyboard never pops up). The server repairs the
  // transcript before returning it, so what lands in the draft is readable
  // rather than raw Whisper output.
  async _startRecording() {
    if (this._recState !== 'idle') return;
    // Tapping the mic pauses any ongoing read-aloud: you are about to speak to
    // Ara, so a previous reply still talking over you is the wrong behaviour —
    // but the place is kept, so the reading can go on afterwards.
    READER.pause();
    const key = this._key();
    // The status row hides the mic while this conversation's own job runs, but
    // guard anyway: one dictation job per conversation at a time.
    if (VOICE_JOBS.has(key)) return;
    if (!canRecord()) {
      this._attachError = 'Voice recording is not supported on this device.';
      this.render();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this._recStream = stream;
      this._recChunks = [];
      this._recIntent = null;
      this._recAborted = false;
      VOICE_ERRORS.delete(key);
      const mr = new MediaRecorder(stream);
      this._mediaRecorder = mr;
      mr.addEventListener('dataavailable', (e) => {
        if (e.data && e.data.size) this._recChunks.push(e.data);
      });
      // The job belongs to the conversation it was recorded in — the key,
      // the seed and the model choice are captured now, so a navigation
      // away cannot re-address it or lend it another composer's context.
      const seed = this._seed();
      const model = this._newModel;
      mr.addEventListener('stop', () => this._onRecordingStopped(key, seed, model));
      mr.start();
      this._recState = 'recording';
      this._attachError = '';
      this.render();
      this._wave.start(stream);
    } catch (_err) {
      this._recState = 'idle';
      this._attachError = 'Microphone access was denied.';
      this._stopStream();
      this.render();
    }
  }

  // Abort: throw the recording away and return to the plain input row.
  _abortRecording() {
    if (this._recState !== 'recording' || this._recIntent || this._recAborted) return;
    this._recAborted = true;
    this._stopRecording();
  }

  // Check / send buttons: stop the recorder with the chosen intent; the actual
  // work continues in _onRecordingStopped once the recorder flushes its chunks.
  // A decision already taken (an earlier tap, or abort) wins over later calls —
  // this is what keeps a navigation right after a ➤ tap from downgrading the
  // intent to 'review'.
  _finishRecording(intent) {
    if (this._recState !== 'recording' || this._recIntent || this._recAborted) return;
    this._recIntent = intent;
    this._stopRecording();
  }

  _stopRecording() {
    try {
      if (this._mediaRecorder && this._mediaRecorder.state !== 'inactive') {
        this._mediaRecorder.stop();
      }
    } catch (_e) { /* ignore */ }
  }

  _stopStream() {
    if (this._recStream) {
      try { this._recStream.getTracks().forEach((tr) => tr.stop()); } catch (_e) { /* ignore */ }
      this._recStream = null;
    }
  }

  // From here on the dictation is a background job of its conversation alone:
  // the recorder is free again, and the transcript lands in that draft — sent
  // from there on the send path — whether or not the conversation is still on
  // screen. Completion must not interrupt whatever the user is doing now: only
  // when the conversation is on screen is it re-rendered, and only the
  // deliberate review flow pulls up the keyboard.
  async _onRecordingStopped(key, seed, model) {
    this._wave.stop();
    this._stopStream();
    const chunks = this._recChunks || [];
    this._recChunks = [];
    const type = (this._mediaRecorder && this._mediaRecorder.mimeType)
      || (chunks[0] && chunks[0].type) || 'audio/webm';
    this._mediaRecorder = null;
    const intent = this._recIntent || 'review';
    this._recIntent = null;
    const aborted = this._recAborted;
    this._recAborted = false;
    this._recState = 'idle';
    if (aborted || !chunks.length) {
      if (this.isConnected) this.render();
      return;
    }
    const blob = new Blob(chunks, { type });
    VOICE_JOBS.set(key, { sending: intent === 'send', phase: 'transcribing' });
    const live = () => LIVE.get(key);
    if (live()) live().render();
    let toSend = '';
    try {
      // The thread is context for the cleanup pass: it is what tells the model
      // which names and topics this dictation is likely to be about. The
      // composer key is not a thread — only a real thread id is sent.
      const q = key !== NEW_KEY ? `?thread=${encodeURIComponent(key)}` : '';
      const res = await fetch(`/conversations/transcribe${q}`, {
        method: 'POST',
        headers: { 'Content-Type': blob.type || 'application/octet-stream' },
        body: blob,
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const text = ((data && data.text) || '').trim();
      if (text) {
        appendToDraft(key, text);
        // Send the whole draft, so anything typed before dictating comes along.
        if (intent === 'send') toSend = draftOf(key).text;
      } else {
        VOICE_ERRORS.set(key, 'No speech was detected in the recording.');
      }
    } catch (_err) {
      VOICE_ERRORS.set(key, "Couldn't transcribe the recording. Please try again.");
    }
    if (toSend) {
      // Send path: the status row stays in place of the textarea until the
      // send completes, so the keyboard never appears. On failure the draft
      // stays in place, which then shows up (unfocused) for a manual retry.
      VOICE_JOBS.get(key).phase = 'sending';
      if (live()) live().render();
      const el = live();
      if (key !== NEW_KEY && el) {
        await el._send(toSend);
      } else {
        // Off screen, or a first message: the send goes out with the context
        // captured when the recording started. The composer on screen now,
        // if any, may be a different one — opened for another project while
        // this was transcribed — and must not lend it its seed or model.
        try {
          const conv = await sendMessage(key === NEW_KEY ? '' : key, toSend,
            draftOf(key).files, seed, model);
          draftOf(key).text = '';
          draftOf(key).files = [];
          // The composer the user is looking at goes on as the new thread
          // only when it is the one this was dictated in; any other stays
          // what it is, and the list's next refresh shows the thread.
          const now = live();
          if (key === NEW_KEY && now && sameSeed(now._seed(), seed)) now._becomeCreated(conv);
        } catch (_err) { /* the draft stays for a manual retry */ }
      }
    }
    VOICE_JOBS.delete(key);
    const el = live();
    if (el) {
      el._focusNext = intent === 'review';
      el.render();
    }
  }

  // ── Model, archive, autoplay ───────────────────────────────────────────────
  // Model dropdown changed. In a new-thread composer it just holds the choice
  // for the thread about to open. In an open thread it is persisted right
  // away (takes effect on the next turn) so a page reload keeps it.
  async _onModelChange(value) {
    const model = value || '';
    if (!this._id) {
      this._newModel = model;
      return;
    }
    // Optimistic: reflect it locally, then persist. On failure the next read
    // restores the server's value.
    if (this._thread) { this._thread.model = model; this._thread.escalated = false; }
    try {
      await fetch(`/conversations/${encodeURIComponent(this._id)}/model`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
    } catch (_err) { /* the next read re-syncs the real value */ }
    await this._load();
    this._partialUpdate();
  }

  async _archive(archived) {
    if (this._busy || !this._id) return;
    this._busy = true;
    try {
      const res = await fetch(`/conversations/${encodeURIComponent(this._id)}/${archived ? 'archive' : 'unarchive'}`,
        { method: 'POST' });
      if (!res.ok) throw new Error(String(res.status));
      if (this._thread) this._thread.archived = archived;
      this._emit('retinue-archived', { id: this._id, archived });
    } catch (_err) {
      // keep the thread open; a later read reconciles state
    } finally {
      this._busy = false;
      if (this.isConnected) this.render();
    }
  }

  _toggleAutoplay() {
    autoplay = !autoplay;
    try { localStorage.setItem(AUTOPLAY_KEY, autoplay ? '1' : '0'); } catch (_e) { /* ignore */ }
    const btn = this.shadowRoot.querySelector('[data-autoplay]');
    if (btn) {
      btn.classList.toggle('on', autoplay);
      btn.setAttribute('aria-pressed', String(autoplay));
      btn.textContent = autoplay ? '\u{1F50A}' : '\u{1F507}';
    }
  }

  // ── Read-aloud over this thread ────────────────────────────────────────────
  // The message's own button: play it, or pause/resume it when it is the one
  // in the player (a resume goes on from where it was, never from the top).
  _onSpeakButton(btn) {
    const idx = Number(btn.dataset.speakIdx);
    const t = this._thread;
    const m = (t && t.messages) ? t.messages[idx] : null;
    if (!m) return;
    if (isLoaded(t.id, m)) { READER.toggle(); return; }
    playMessage(t, m, 0);
  }

  // Bring the message buttons in line with the reader, in place.
  _applyReading() {
    const root = this.shadowRoot;
    const t = this._thread;
    if (!root || !t) return;
    root.querySelectorAll('.speak[data-speak-idx]').forEach((btn) => {
      const m = (t.messages || [])[Number(btn.dataset.speakIdx)];
      if (!m) return;
      const loaded = isLoaded(t.id, m);
      const playing = loaded && READER.speaking;
      const label = playing ? 'Pause' : (loaded ? 'Resume reading' : 'Read aloud');
      const glyph = playing ? '⏸' : (loaded ? '▶' : '\u{1F50A}');
      if (btn.textContent !== glyph) btn.textContent = glyph;
      btn.title = label;
      btn.setAttribute('aria-label', label);
      btn.classList.toggle('on', loaded);
      const msg = btn.closest('.msg');
      if (msg) msg.classList.toggle('reading', loaded);
    });
  }

  // A reading interrupted by leaving the page is offered again when its thread
  // opens: the message goes into the player, paused, at the passage it was in.
  // Only when the player is free — a reading in progress is never displaced.
  _restorePosition(t) {
    if (!t || READER.loaded || !speechAvailable()) return;
    const saved = savedPosition();
    if (!saved || saved.conv !== t.id) return;
    const m = (t.messages || []).find((x) => x.role !== 'user' && x.ts === saved.ts);
    if (!m) return;
    const clean = plainForSpeech(m.text);
    if (!clean) return;
    PLAYING = playingInfo(t, m);
    READER.load([{ id: m.ts, lang: m.lang, text: clean }], { fraction: saved.fraction });
  }

  // When autoplay is on, speak assistant messages that arrive after the thread
  // was opened. The first look at a thread only records its existing messages
  // as "seen" so historical replies are never blurted out on open.
  _maybeAutoplay(t) {
    if (!t || !speechAvailable()) return;
    const cid = t.id;
    if (!SPOKEN.has(cid)) SPOKEN.set(cid, new Set());
    const seen = SPOKEN.get(cid);
    const replies = (t.messages || []).filter((m) => m.role !== 'user' && (m.text || '').trim());
    if (!AUTO_READY.get(cid)) {
      replies.forEach((m) => seen.add(m.ts));
      AUTO_READY.set(cid, true);
      return;
    }
    const fresh = replies.filter((m) => !seen.has(m.ts));
    fresh.forEach((m) => seen.add(m.ts));
    if (!autoplay || !fresh.length) return;
    playMessage(t, fresh[fresh.length - 1], 0);
  }

  // ── Wiring ─────────────────────────────────────────────────────────────────
  _wire() {
    const root = this.shadowRoot;
    const back = root.querySelector('[data-back]');
    if (back) back.addEventListener('click', () => this._emit('retinue-back', { id: this._id }));
    const arch = root.querySelector('[data-archive]');
    if (arch) arch.addEventListener('click', () => this._archive(true));
    const unarch = root.querySelector('[data-unarchive]');
    if (unarch) unarch.addEventListener('click', () => this._archive(false));
    // Copy buttons, chips and speak buttons: delegated on the thread container,
    // which survives the in-place message swaps of a poll, so one listener
    // covers every bubble ever rendered here.
    const threadEl = root.querySelector('.thread');
    if (threadEl) {
      threadEl.addEventListener('click', (e) => {
        const btn = e.target.closest('.copy');
        if (btn) { copyToClipboard(btn, root); return; }
        const chip = e.target.closest('.md-chip');
        if (chip) { this._fillComposer(chip.getAttribute('data-fill') || ''); return; }
        const sbtn = e.target.closest('.speak');
        if (sbtn) this._onSpeakButton(sbtn);
      });
    }
    const mic = root.querySelector('[data-mic]');
    if (mic) mic.addEventListener('click', () => this._startRecording());
    const recAbort = root.querySelector('[data-rec-abort]');
    if (recAbort) recAbort.addEventListener('click', () => this._abortRecording());
    const recCheck = root.querySelector('[data-rec-check]');
    if (recCheck) recCheck.addEventListener('click', () => this._finishRecording('review'));
    const recSend = root.querySelector('[data-rec-send]');
    if (recSend) recSend.addEventListener('click', () => this._finishRecording('send'));
    const modelSel = root.querySelector('[data-model]');
    if (modelSel) modelSel.addEventListener('change', () => this._onModelChange(modelSel.value));
    const ap = root.querySelector('[data-autoplay]');
    if (ap) ap.addEventListener('click', () => this._toggleAutoplay());
    const fileInput = root.querySelector('[data-file]');
    if (fileInput) {
      fileInput.addEventListener('change', () => {
        // Snapshot the picked files into an array *before* resetting the input.
        // `fileInput.files` is a live FileList; setting `value = ''` (done so the
        // same file can be re-picked after removal) empties that very list, so
        // reading it afterwards yields zero files and no attachment ever appears.
        const picked = Array.from(fileInput.files || []);
        fileInput.value = '';  // allow re-picking the same file after removal
        this._addFiles(picked);
      });
    }
    root.querySelectorAll('[data-rmfile]').forEach((el) =>
      el.addEventListener('click', () => this._removeFile(Number(el.getAttribute('data-rmfile')))));
    const form = root.querySelector('[data-form]');
    if (form) {
      const input = form.querySelector('textarea');
      const grow = () => {
        input.style.height = 'auto';
        input.style.height = `${Math.min(input.scrollHeight, Math.round(window.innerHeight * TEXTAREA_MAX_HEIGHT_RATIO))}px`;
      };
      // The draft follows the keystrokes, so a re-render never wipes it (the
      // input's value is rebuilt from the draft on each render) and a return
      // to the thread finds it.
      input.addEventListener('input', () => {
        draftOf(this._key()).text = input.value;
        grow();
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          form.requestSubmit();
        }
      });
      grow();
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        const text = input.value;
        if (text.trim() || draftOf(this._key()).files.length) this._send(text);
      });
      // Restore focus and caret after a re-render so typing isn't interrupted,
      // but only when the field already had focus or the view was just opened —
      // a background re-render must not steal focus or pop the keyboard.
      const wantFocus = (this._hadFocus || this._focusNext) && !this._busy;
      this._focusNext = false;
      if (wantFocus) {
        setTimeout(() => {
          if (!input.isConnected) return;
          input.focus();
          const end = input.value.length;
          try { input.setSelectionRange(end, end); } catch (_err) { /* ignore */ }
        }, 0);
      }
    }
  }
}

// Two composers are about the same thing when they carry the same project.
function sameSeed(a, b) {
  return String((a && a.project) || '') === String((b && b.project) || '');
}

// One message onto the wire: a reply into thread `id`, or — with no id — the
// first message that opens a thread, linked to the project in `seed` and
// pinned to `model` when one was picked. Returns the thread as the gateway
// answers it.
async function sendMessage(id, text, files, seed, model) {
  const body = { message: text };
  if (!id) {
    if (seed && seed.project) {
      body.project = seed.project;
      if (seed.project_title) body.project_title = seed.project_title;
    }
    if (model) body.model = model;
  }
  if (files && files.length) {
    body.attachments = files.map((f) => ({ filename: f.name, content_type: f.type, data: f.data }));
  }
  const url = id ? `/conversations/${encodeURIComponent(id)}/messages` : LIST_URL;
  const res = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

const BAR_CSS = `
  :host { display: block; flex: none; }
  * { box-sizing: border-box; }
  button { font: inherit; }
  button:focus-visible { outline: 2px solid var(--accent, #6ea8fe); outline-offset: 1px; }
  .player { display: flex; align-items: center; gap: 4px; margin-top: 4px; padding: 8px 0 4px;
            border-top: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .p-btn { flex: none; width: 36px; height: 36px; border-radius: 50%; border: 0;
           background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); cursor: pointer;
           font-size: .95rem; line-height: 1; display: inline-flex; align-items: center;
           justify-content: center; padding: 0; -webkit-tap-highlight-color: transparent; }
  .p-btn:hover { background: rgba(110, 168, 254, .2); }
  .p-btn:active { filter: brightness(1.12); }
  .p-main { background: var(--accent, #6ea8fe); color: #0b0d12; font-size: 1.05rem; }
  .p-main:hover { background: var(--accent, #6ea8fe); filter: brightness(1.08); }
  .p-close { background: transparent; color: var(--muted, #8b93a3); }
  .p-track { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0; padding: 0 4px; }
  /* The slider is the position bar: the played part in accent, the rest in a
     faint line (--p is set from script), with a thumb big enough to grab. */
  .p-seek { -webkit-appearance: none; appearance: none; width: 100%; height: 28px; margin: 0;
            background: transparent; cursor: pointer; touch-action: pan-y; }
  .p-seek::-webkit-slider-runnable-track { height: 4px; border-radius: 2px;
    background: linear-gradient(to right, var(--accent, #6ea8fe) var(--p, 0%),
                                rgba(231, 235, 242, .18) var(--p, 0%)); }
  .p-seek::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 16px; height: 16px;
    border-radius: 50%; background: var(--accent, #6ea8fe); border: 0; margin-top: -6px; }
  .p-seek::-moz-range-track { height: 4px; border-radius: 2px; background: rgba(231, 235, 242, .18); }
  .p-seek::-moz-range-progress { height: 4px; border-radius: 2px; background: var(--accent, #6ea8fe); }
  .p-seek::-moz-range-thumb { width: 16px; height: 16px; border-radius: 50%;
    background: var(--accent, #6ea8fe); border: 0; }
  .p-info { display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
            font-size: .72rem; color: var(--muted, #8b93a3); margin-top: -4px; }
  .p-who { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .p-link { background: transparent; border: 0; padding: 0; color: var(--accent, #6ea8fe);
            font: inherit; cursor: pointer; text-align: left; }
  .p-link:hover { text-decoration: underline; }
  .p-pos { flex: none; font-variant-numeric: tabular-nums; white-space: nowrap; }
`;

const CSS = `
  :host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
  * { box-sizing: border-box; }
  button { font: inherit; }
  button:focus-visible, a:focus-visible, textarea:focus-visible {
    outline: 2px solid var(--accent, #6ea8fe); outline-offset: 1px; }
  .conv { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .muted { color: var(--muted, #8b93a3); }

  /* ── Bar ───────────────────────────────────────────────────────────────── */
  .thread-bar { flex: none; display: flex; align-items: center; gap: 10px; padding: 2px 0 10px;
                border-bottom: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .back { flex: none; width: 34px; height: 34px; border-radius: 50%; border: 0;
          background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); cursor: pointer;
          font-size: 1.35rem; line-height: 1; display: inline-flex; align-items: center;
          justify-content: center; padding: 0 2px 2px 0; -webkit-tap-highlight-color: transparent; }
  .bar-title { flex: 1; min-width: 0; font-weight: 650; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }
  .bar-actions { flex: none; display: inline-flex; align-items: center; gap: 6px; }
  .iconbtn { width: 34px; height: 34px; border-radius: 50%; background: transparent;
             border: 1px solid var(--line, rgba(231, 235, 242, .08)); color: var(--muted, #8b93a3);
             cursor: pointer; font-size: .95rem; display: inline-flex; align-items: center;
             justify-content: center; padding: 0; }
  .iconbtn:hover { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }
  .iconbtn.on { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }
  .pill { background: transparent; border: 1px solid var(--line, rgba(231, 235, 242, .08));
          border-radius: 999px; color: var(--muted, #8b93a3); cursor: pointer;
          padding: 6px 12px; font-size: .78rem; white-space: nowrap; }
  .pill:hover { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }
  .model-pick { flex: none; display: inline-flex; align-items: center; gap: 3px;
                color: var(--muted, #8b93a3); }
  .model-pick .mp-ico { font-size: .9rem; line-height: 1; }
  .model-pick select { background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2);
                       border: 1px solid var(--line, rgba(231, 235, 242, .08));
                       border-radius: 999px; padding: 5px 8px; font-size: .74rem;
                       max-width: 9.5rem; cursor: pointer; -webkit-appearance: none;
                       appearance: none; }
  .model-pick select:hover { border-color: var(--accent, #6ea8fe); }
  /* The composer's roomy form: a captioned, untruncated picker centered under
     the "Ask Ara anything" hint, so the model choice is plainly offered before
     the conversation starts. */
  .model-pick.wide { gap: 8px; margin-top: 14px; }
  .model-pick.wide .mp-label { font-size: .8rem; }
  .model-pick.wide select { max-width: none; font-size: .85rem; padding: 7px 12px; }

  /* ── New-thread composer body ──────────────────────────────────────────── */
  .about-chip { flex: none; align-self: flex-start; margin-top: 10px; padding: 5px 12px;
                border-radius: 999px; background: var(--card-2, #1c2230);
                border: 1px solid var(--accent, #6ea8fe); color: var(--fg, #e7ebf2);
                font-size: .78rem; font-weight: 600; max-width: 100%;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
           gap: 6px; color: var(--muted, #8b93a3); text-align: center; padding: 24px 12px; }
  .empty .e-ico { font-size: 2rem; opacity: .55; }
  .empty p { margin: 0; max-width: 32ch; }

  /* ── Thread ────────────────────────────────────────────────────────────── */
  .thread { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
            display: flex; flex-direction: column; gap: 12px; padding: 12px 2px; }
  /* An open thread takes the whole frame, which on a wide display is far wider
     than a comfortable line. Centre the messages and the composer in a reading
     column; the bar keeps its full-width divider. */
  @media (min-width: 1000px) {
    .thread, .composer, retinue-read-aloud {
      width: 100%; max-width: 900px; margin-left: auto; margin-right: auto; }
  }
  .msg { display: flex; flex-direction: column; gap: 3px; max-width: 86%; }
  .msg.me { align-self: flex-end; align-items: flex-end; }
  .who { color: var(--muted, #8b93a3); font-size: .7rem; }
  /* The quiet header meta after the sender name: model · ~$cost · time. All one
     muted, small line so it never competes with the message body. */
  .msg-meta { color: var(--muted, #8b93a3); font-size: .7rem;
              display: inline-flex; align-items: baseline; gap: 5px; flex-wrap: wrap; }
  .msg-meta .m-sep { opacity: .5; }
  .msg-meta .m-cost { font-variant-numeric: tabular-nums; }
  .msg-meta .m-model { font-weight: 600; }
  /* Message text is rendered by the shared Markdown renderer (its .md styles
     are appended after this sheet), so the bubble needs no pre-wrap: block
     structure comes from the renderer. */
  .bubble { background: var(--card-2, #1c2230); border-radius: 16px; padding: 9px 13px;
            line-height: 1.4; }
  .msg.ara .bubble, .msg.agent .bubble { border-bottom-left-radius: 6px; }
  .msg.me .bubble { background: var(--accent, #6ea8fe); color: #0b0d12; border-bottom-right-radius: 6px; }
  .msg.agent .bubble { border: 1px solid var(--accent, #6ea8fe); }
  .bubble a { color: var(--accent, #6ea8fe); text-decoration: underline; overflow-wrap: anywhere; }
  .msg.me .bubble .md a, .msg.me .bubble a { color: #0b0d12; }
  .msg.me .bubble .md code, .msg.me .bubble code { background: rgba(11, 13, 18, .15); }
  .attachments { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
  .attach-row { display: flex; align-items: stretch; gap: 6px; }
  .attach-row .attach { flex: 1 1 auto; }
  .a-dl { flex: none; display: flex; align-items: center; padding: 0 11px; border-radius: 8px;
          border: 1px solid var(--accent, #6ea8fe); background: rgba(110, 168, 254, .1);
          color: inherit; text-decoration: none; font-size: .9rem; }
  .a-dl:hover { background: rgba(110, 168, 254, .2); }
  .msg.me .a-dl { border-color: rgba(11, 13, 18, .4); background: rgba(11, 13, 18, .12); }
  .attach { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: 8px;
            border: 1px solid var(--accent, #6ea8fe); background: rgba(110, 168, 254, .1);
            color: inherit; text-decoration: none; font-size: .82rem; white-space: normal; }
  .attach:hover { background: rgba(110, 168, 254, .2); }
  .attach .a-icon { flex: none; }
  .attach .a-name { flex: 1 1 auto; overflow-wrap: anywhere; }
  .attach .a-size { flex: none; color: var(--muted, #8b93a3); font-size: .72rem; }
  .msg.me .attach { border-color: rgba(11, 13, 18, .4); background: rgba(11, 13, 18, .12); }
  .msg.me .attach .a-size { color: rgba(11, 13, 18, .7); }
  .quote { margin: 6px 0; padding: 8px 10px; border-left: 3px solid var(--accent, #6ea8fe);
           background: rgba(110, 168, 254, .1); border-radius: 8px;
           display: flex; flex-direction: column; gap: 6px; }
  .quote:first-child { margin-top: 0; }
  .quote:last-child { margin-bottom: 0; }
  .q-text { white-space: pre-wrap; line-height: 1.4; }
  .copy { align-self: flex-end; background: var(--accent, #6ea8fe); color: #0b0d12; border: 0;
          border-radius: 8px; padding: 3px 10px; font: inherit; font-size: .74rem; font-weight: 600;
          cursor: pointer; }
  .copy.done { background: var(--ok, #57c785); }
  .code-wrap { position: relative; }
  .code-wrap .md-pre { margin: 6px 0; }
  .code-copy { position: absolute; top: 6px; right: 6px; padding: 2px 8px; font-size: .7rem;
               opacity: .85; }
  .code-wrap:hover .code-copy, .code-copy:focus { opacity: 1; }
  .bubble.pending { color: var(--muted, #8b93a3); font-style: italic; }
  .pending-help { display: block; margin-top: 4px; font-size: .72rem; line-height: 1.35; color: var(--muted, #8b93a3); }
  .msg-head { display: flex; align-items: center; gap: 6px; }
  .msg.me .msg-head { flex-direction: row-reverse; }
  /* A finger-sized target that still sits in a one-line header: the negative
     margin lets the 30px hit area overhang the small text around it. */
  .speak { background: transparent; border: 0; cursor: pointer; padding: 0; margin: -7px 0;
           width: 30px; height: 30px; border-radius: 50%; display: inline-flex; align-items: center;
           justify-content: center; font-size: .85rem; line-height: 1; opacity: .65;
           color: inherit; -webkit-tap-highlight-color: transparent; }
  .speak:hover { opacity: 1; }
  .speak.on { opacity: 1; color: var(--accent, #6ea8fe); background: rgba(110, 168, 254, .12); }
  .msg.reading .bubble { outline: 1px solid var(--accent, #6ea8fe); }

  /* ── Composer ──────────────────────────────────────────────────────────── */
  .composer { flex: none; margin-top: 4px; padding-top: 10px;
              border-top: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .row { display: flex; gap: 6px; align-items: flex-end; }
  .field { flex: 1; min-width: 0; position: relative; display: flex; }
  .row textarea { flex: 1; min-width: 0; min-height: 40px; max-height: 35vh; background: var(--card-2, #1c2230);
                 border: 0; border-radius: 20px; padding: 9px 42px 9px 14px; color: var(--fg, #e7ebf2);
                 font: inherit; line-height: 1.35; resize: none; overflow-y: auto; }
  .row textarea::placeholder { color: var(--muted, #8b93a3); }
  .row textarea:focus-visible { outline: 1px solid rgba(110, 168, 254, .45); outline-offset: 0; }
  .row button[type="submit"] { flex: none; display: inline-flex; align-items: center; justify-content: center;
                width: 40px; height: 40px; border-radius: 50%; background: var(--accent, #6ea8fe);
                color: #0b0d12; border: 0; font-size: 1.05rem; cursor: pointer; padding: 0 0 0 2px;
                -webkit-tap-highlight-color: transparent; }
  /* The attach control sits inside the text field's bottom-right corner, so it
     costs the row no width of its own. */
  .clip { position: absolute; right: 3px; bottom: 3px; display: inline-flex; align-items: center;
          justify-content: center; height: 34px; width: 34px; border-radius: 50%;
          background: transparent; color: var(--muted, #8b93a3); cursor: pointer;
          font-size: 1rem; user-select: none; -webkit-tap-highlight-color: transparent; }
  .clip:hover { background: rgba(110, 168, 254, .2); }
  /* Mic button, recording row and status row styles come from the shared
     VOICE_CSS (voice.js), appended to this sheet in render(). */
  .row button[disabled], .row textarea[disabled], .clip:has(input[disabled]) { opacity: .6; cursor: default; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; padding: 4px 6px 4px 10px;
          border-radius: 999px; background: var(--card-2, #1c2230); border: 1px solid var(--accent, #6ea8fe);
          font-size: .78rem; }
  .chip .c-name { overflow-wrap: anywhere; }
  .chip .c-size { color: var(--muted, #8b93a3); font-size: .7rem; }
  .chip .c-x { background: none; border: 0; color: var(--muted, #8b93a3); cursor: pointer;
               font-size: 1rem; line-height: 1; padding: 0 2px; }
  .chip .c-x:hover { color: var(--high, #ff6b6b); }
  .attach-err { color: var(--high, #ff6b6b); font-size: .76rem; margin-bottom: 8px; }
`;

customElements.define('retinue-read-aloud', RetinueReadAloud);
customElements.define('retinue-conversation', RetinueConversation);
