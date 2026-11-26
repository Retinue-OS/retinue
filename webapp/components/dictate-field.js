// Dictation for any text field: the rule is that wherever the dashboard asks
// for words, they can be spoken instead of typed (docs/dashboard.md, "Every
// text field takes dictation").
//
// The big composers (conversation, chat, project command bar) keep their own
// recording row — waveform, review ✓, send ➤ — since what "send" means is
// theirs. Every other field gets this: a small 🎤 in the field's corner.
// Tap it and it records (it turns into a red ■ with the time running, and a
// ✕ beside it throws the take away); tap ■ and the words are transcribed and
// appended to what the field already holds, as if typed — the field's own
// `input` event fires, so the host keeps its state exactly as for typing.
// Nothing is ever sent by a dictation here: the host's own button does that.
//
// What a host does:
//   - put the field and the button in a `.dict-field` box:
//       `<div class="dict-field">` +
//         `<textarea data-dictate-field="triage-feedback" …></textarea>` +
//         dictateButtonHtml('triage-feedback', 'Dictate feedback') +
//       `</div>`
//     (`.dict-field.line` for a one-line input field: the button sits centred);
//   - include DICTATE_CSS in its shadow styles;
//   - call wireDictation(this.shadowRoot) after every render.
//
// A key names the field's place and should be unique per thing edited
// (`contact-instruction:<id>`, not just `contact-instruction`): the
// transcription runs in the service worker (dictation.js), so a page left
// mid-transcription gets the text back into that same field when the place is
// shown again — and only into that one.
//
// State is kept here, per key, not in the host: a host re-render in the
// middle of a recording draws the button as it is (recording, transcribing)
// rather than losing it.

import { canRecord, micErrorText } from './voice.js';
import { transcribe, handOff, adopt } from './dictation.js';

const TRANSCRIBE_URL = '/conversations/transcribe';
const SLICE_MS = 250;
// A field is no place for a speech: a take that runs this long is stopped and
// transcribed as it is.
const MAX_MS = 3 * 60 * 1000;
const MSG_MS = 6000;

// key -> {phase: 'idle'|'recording'|'working', msg, started}
const STATE = new Map();
// Text a job brought back while its field was not on screen: put in once it is.
const PENDING = new Map();
const ROOTS = new Set();
const WIRED = new WeakSet();
const ADOPTED = new Set();
// key -> {start, end, value}: where the caret was when the field was left
// (for the mic, most often), so a dictation goes where the user was writing.
const CARET = new Map();
let rec = null;   // the one live recording: {key, mr, stream, chunks, aborted, timer, tick}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function stateOf(key) {
  return STATE.get(key) || { phase: 'idle', msg: '', started: 0 };
}

function setState(key, patch) {
  const s = { ...stateOf(key), ...patch };
  STATE.set(key, s);
  if (patch.msg) {
    clearTimeout(s.msgTimer);
    s.msgTimer = setTimeout(() => setState(key, { msg: '' }), MSG_MS);
  }
  paint(key);
}

function clock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function innerHtml(key, label) {
  const s = stateOf(key);
  const k = esc(key);
  const msg = s.msg ? `<span class="dict-msg" role="status">${esc(s.msg)}</span>` : '';
  if (s.phase === 'recording') {
    return `<span class="dict-msg dict-live" role="status"><span class="dict-dot" aria-hidden="true"></span>` +
      `<span data-dictate-time>${clock(Date.now() - s.started)}</span></span>` +
      `<button type="button" class="dict-btn dict-abort" data-dictate-abort="${k}" ` +
      `title="Discard recording" aria-label="Discard recording">&#10005;</button>` +
      `<button type="button" class="dict-btn dict-stop" data-dictate="${k}" ` +
      `title="Stop and transcribe" aria-label="Stop and transcribe">&#9632;</button>`;
  }
  if (s.phase === 'working') {
    return `<span class="dict-msg" role="status">Transcribing…</span>` +
      `<button type="button" class="dict-btn" disabled aria-label="Transcribing">&#8230;</button>`;
  }
  return msg + `<button type="button" class="dict-btn" data-dictate="${k}" ` +
    `title="${esc(label)}" aria-label="${esc(label)}">\u{1F3A4}</button>`;
}

// The button (and its state line) for the field `[data-dictate-field=key]`.
// Empty where the browser cannot record: the field is then a plain field.
export function dictateButtonHtml(key, label = 'Dictate') {
  let can = false;
  try { can = canRecord(); } catch (_e) { /* no browser here */ }
  if (!can) return '';
  return `<span class="dict" data-dictate-wrap="${esc(key)}" data-dictate-label="${esc(label)}">` +
    `${innerHtml(key, label)}</span>`;
}

function each(sel, fn) {
  for (const root of [...ROOTS]) {
    if (root.isConnected === false) { ROOTS.delete(root); continue; }
    root.querySelectorAll(sel).forEach(fn);
  }
}

function sel(attr, key) {
  return `[${attr}="${String(key).replace(/["\\]/g, '\\$&')}"]`;
}

function paint(key) {
  each(sel('data-dictate-wrap', key), (w) => {
    w.innerHTML = innerHtml(key, w.getAttribute('data-dictate-label') || 'Dictate');
  });
}

function field(key) {
  let found = null;
  each(sel('data-dictate-field', key), (f) => { if (!found) found = f; });
  return found;
}

// Put `text` into the field as typing would, and let the host hear it: at the
// caret the user left in it (CARET, below) when the field still holds what it
// held then, else after what is there.
function insert(key, text) {
  let t = String(text || '').trim();
  if (!t) return;
  const f = field(key);
  // A one-line field holds a name, a subject, a search: the full stop the
  // transcriber puts after every utterance does not belong there.
  if (f && f.tagName === 'INPUT') t = t.replace(/[.。]+$/u, '');
  if (!f) { PENDING.set(key, PENDING.has(key) ? `${PENDING.get(key)} ${t}` : t); return; }
  const value = String(f.value || '');
  const c = CARET.get(key);
  CARET.delete(key);
  let head = value.replace(/\s*$/, '');
  let tail = '';
  if (c && c.value === value && Number.isInteger(c.start) && Number.isInteger(c.end) && c.end < value.length) {
    head = value.slice(0, c.start);
    tail = value.slice(c.end);
  }
  const lead = head && !/\s$/.test(head) ? ' ' : '';
  const trail = tail && !/^\s/.test(tail) ? ' ' : '';
  let next = `${head}${lead}${t}${trail}${tail}`;
  const max = Number(f.getAttribute('maxlength')) || 0;
  if (max > 0 && next.length > max) next = next.slice(0, max);
  f.value = next;
  const at = Math.min(next.length, head.length + lead.length + t.length);
  try { f.setSelectionRange(at, at); } catch (_e) { /* not a text input */ }
  f.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
  // What a typed field fires when it is left: hosts that save on it hear it.
  f.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
}

function settle(key, promise) {
  setState(key, { phase: 'working', msg: '' });
  promise.then((text) => {
    setState(key, { phase: 'idle', msg: text ? '' : 'No speech was heard.' });
    if (text) insert(key, text);
  }, (err) => {
    // 'elsewhere': another page took it into its field meanwhile — not ours.
    setState(key, { phase: 'idle', msg: err && err.message === 'elsewhere' ? '' : 'Could not transcribe — try again.' });
  });
}

async function start(key) {
  if (rec) return;
  if (!canRecord()) { setState(key, { msg: 'Recording is not supported on this device.' }); return; }
  const own = { key, mr: null, stream: null, chunks: [], aborted: false, timer: null, tick: null };
  rec = own;
  try {
    own.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    own.mr = new MediaRecorder(own.stream);
  } catch (err) {
    stopStream(own);
    rec = null;
    setState(key, { phase: 'idle', msg: micErrorText(err) });
    return;
  }
  own.mr.addEventListener('dataavailable', (e) => { if (e.data && e.data.size) own.chunks.push(e.data); });
  own.mr.addEventListener('stop', () => stopped(own));
  own.mr.start(SLICE_MS);
  own.timer = setTimeout(() => stop(false), MAX_MS);
  own.tick = setInterval(() => {
    const s = stateOf(key);
    each(`${sel('data-dictate-wrap', key)} [data-dictate-time]`, (el) => { el.textContent = clock(Date.now() - s.started); });
  }, 500);
  setState(key, { phase: 'recording', msg: '', started: Date.now() });
}

function stop(aborted) {
  if (!rec || !rec.mr) return;
  rec.aborted = rec.aborted || aborted;
  try { if (rec.mr.state !== 'inactive') rec.mr.stop(); } catch (_e) { /* stopped already */ }
}

function stopStream(own) {
  clearTimeout(own.timer);
  clearInterval(own.tick);
  if (own.stream) {
    try { own.stream.getTracks().forEach((t) => t.stop()); } catch (_e) { /* ignore */ }
    own.stream = null;
  }
}

function blobOf(own) {
  const type = (own.mr && own.mr.mimeType) || (own.chunks[0] && own.chunks[0].type) || 'audio/webm';
  return new Blob(own.chunks, { type });
}

function stopped(own) {
  stopStream(own);
  if (rec === own) rec = null;
  if (own.aborted || !own.chunks.length) { setState(own.key, { phase: 'idle' }); return; }
  settle(own.key, transcribe(own.key, blobOf(own), TRANSCRIBE_URL));
}

// Leaving with the mic live: the take so far goes to the worker, and its text
// waits for this field's place (adopt, below).
if (typeof addEventListener === 'function') addEventListener('pagehide', () => {
  if (!rec || !rec.mr) return;
  const own = rec;
  rec = null;
  own.aborted = true;
  try { if (own.mr.state !== 'inactive') own.mr.requestData(); } catch (_e) { /* ignore */ }
  if (own.chunks.length) handOff(own.key, blobOf(own), TRANSCRIBE_URL);
  stopStream(own);
});

// After every render of a host: the buttons work, a field that came back on
// screen takes what was transcribed for it meanwhile, and the place of every
// field is claimed for jobs a page left behind.
export function wireDictation(root) {
  if (!root) return;
  ROOTS.add(root);
  if (!WIRED.has(root) && typeof root.addEventListener === 'function') {
    WIRED.add(root);
    root.addEventListener('click', (e) => {
      const t = e.target && e.target.closest ? e.target : null;
      if (!t) return;
      const ab = t.closest('[data-dictate-abort]');
      const go = t.closest('[data-dictate]');
      if (!ab && !go) return;
      e.preventDefault();
      e.stopPropagation();
      if (ab) { if (rec && rec.key === ab.getAttribute('data-dictate-abort')) stop(true); return; }
      const key = go.getAttribute('data-dictate');
      if (rec && rec.key === key) stop(false);
      else if (!rec) start(key);
    });
    root.addEventListener('focusout', (e) => {
      const f = e.target;
      const key = f && f.getAttribute && f.getAttribute('data-dictate-field');
      if (!key) return;
      try {
        CARET.set(key, { start: f.selectionStart, end: f.selectionEnd, value: String(f.value || '') });
      } catch (_e) { /* no selection on this kind of field */ }
    });
  }
  root.querySelectorAll('[data-dictate-field]').forEach((f) => {
    const key = f.getAttribute('data-dictate-field');
    if (PENDING.has(key)) {
      const t = PENDING.get(key);
      PENDING.delete(key);
      insert(key, t);
    }
    if (!ADOPTED.has(key)) {
      ADOPTED.add(key);
      adopt(key, (p) => settle(key, p));
    }
  });
}

export const DICTATE_CSS = `
  .dict-field { position: relative; display: block; min-width: 0; }
  .dict-field > textarea, .dict-field > input { box-sizing: border-box; padding-right: 40px !important; }
  .dict { position: absolute; right: 4px; bottom: 6px; display: flex; align-items: center; gap: 4px;
          pointer-events: none; }
  .dict-field:not(.line) > textarea + .dict { right: 14px; }
  .dict-field.line .dict { bottom: auto; top: 50%; transform: translateY(-50%); }
  .dict-field.inline { display: inline-block; vertical-align: middle; }
  .dict > * { pointer-events: auto; }
  .dict-btn { display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 30px;
              flex: none; border-radius: 50%; border: 0; padding: 0; cursor: pointer; font-size: .9rem;
              background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); user-select: none;
              -webkit-tap-highlight-color: transparent; }
  .dict-btn:hover { background: rgba(110, 168, 254, .2); }
  .dict-btn:focus-visible { outline: 2px solid var(--fg, #e7ebf2); outline-offset: 1px; }
  .dict-btn[disabled] { opacity: .6; cursor: default; }
  .dict-stop { background: var(--high, #ff6b6b); color: #fff; }
  .dict-stop:hover { background: var(--high, #ff6b6b); filter: brightness(1.1); }
  .dict-abort { color: var(--high, #ff6b6b); }
  .dict-msg { display: inline-flex; align-items: center; gap: 6px; max-width: 220px; padding: 3px 8px;
              border-radius: 10px; background: var(--card, #151922); color: var(--muted, #8b93a3);
              font-size: .72rem; line-height: 1.25; font-variant-numeric: tabular-nums; }
  .dict-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--high, #ff6b6b);
              animation: dict-pulse 1.2s ease-in-out infinite; }
  @keyframes dict-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .5; } }
`;
