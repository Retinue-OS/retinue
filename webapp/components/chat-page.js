// <retinue-chat-page>: one messenger chat (chat.html?id=<chat id>) — the
// deterministic mirror of a channel conversation rendered like the messenger
// client itself, with its companion pane (the conversation-with-Ara rail)
// beside it.
//
// Two panes, one page:
//  - phone: the panes sit in a horizontal scroll-snap strip — swipe between
//    Chat and Ara, with a two-tab header as the visible affordance/indicator;
//  - wide frame (WIDE_FRAME from base.js): both panes side by side behind a
//    draggable splitter. The splitter is page-local on purpose — layout.js is
//    the dashboard's splitter manager and knows index.html's regions; if a
//    third page grows one of these, the pointer/persist logic should be lifted
//    into a shared module rather than copied again.
//
// Data comes from the chat API (webapp/README.md, "Messenger chats"):
// GET /chats names each chat's message document in `messages`, and this page
// follows that URL verbatim — it never constructs message URLs. The composer
// is live: sends POST /chats/<id>/send (the user's send press is direct under
// every policy category), the shared draft is saved through the
// version-guarded POST /chats/<id>/draft, and the read watermark advances via
// POST /chats/<id>/read. The open chat is polled on the conversations cadence,
// appending only unseen messages — the composer and the scroll position are
// never rebuilt by a poll. The messages endpoint also pages older history
// (?before=<ts>); this page renders the newest page and leaves a load-older
// affordance for later.
//
// Media: image attachments render inline at their true aspect ratio when the
// record carries intrinsic dimensions (a fixed placeholder frame otherwise —
// either way the box is reserved before the bytes arrive, so a lazy load can
// never shift the thread) and open full screen in a lightbox; audio renders
// as a player above the transcript text (voice notes), video as an inline
// player under the same box-reserve rules. The composer stages images too:
// picked photos (the phone's own chooser offers the camera among the sources)
// are downscaled client-side, previewed above the input row, and sent as the
// `images` part of POST /chats/<id>/send.
//
// The companion pane is the chat's own conversation with Ara: an ordinary
// dashboard conversation (kind `companion`), named by `companion` on the chat
// summary and driven through the same /conversations endpoints the
// conversations card uses. It is created lazily — POST /chats/<id>/companion
// on the user's first turn or chip, never on merely opening a chat, so
// glancing at chats leaves no empty threads behind. Its bar carries the same
// per-thread model picker as the conversation thread bar (GET
// /conversation-models, POST /conversations/<id>/model), so which tier
// answers here is visible and switchable without leaving the chat.
//
// The pane deliberately does NOT embed components/conversations.js: that
// element owns location.hash routing (#conversation-…), polls the list
// endpoint, and flags itself via data-view so styles.css hides the rest of the
// page — none of which can host a side-by-side pane without refactoring it.
// Instead it replicates the conversation thread's visual language (same bubble
// classes and styles, the shared Markdown renderer), so both render
// identically. If a third surface ever needs a thread view, that is the point
// to extract one from conversations.js rather than replicate again.
//
// The two rails close their loop in the shared draft: the user asks here, Ara
// stages a reply into the chat's draft, and the chat poll adopts it into an
// empty composer marked as hers — the send press stays the user's.

import { esc, WIDE_FRAME } from './base.js';
import { renderMarkdown, MD_CSS } from './markdown.js';
import { canRecord, recordingRowHtml, statusRowHtml, Waveform, VOICE_CSS } from './voice.js';
import { avatarHtml, colorFor, CHANNELS } from './chats.js';

const LIST_URL = '/chats';
// Where the back control lands a visitor who has no app history behind them.
const CHATS_URL = '/chats.html';
// Splitter persistence, per device — same pattern as layout.js (STORE_KEY).
const STORE_KEY = 'retinue.chatpage.v1';
const MIN_COMP_PX = 280;      // keep in sync with .pane-companion min-width
const MAX_COMP_FRACTION = 0.6;
const KEY_STEP_PX = 32;
const TEXTAREA_MAX_HEIGHT_RATIO = 0.35;
const NOTE_MS = 2600;
// The conversations cadence: how often the open chat re-fetches its messages.
const POLL_MS = 4000;
// While Ara's turn runs the companion is polled tighter, so her answer lands
// close to when it was written; otherwise it rides the cadence above.
const COMP_PENDING_POLL_MS = 1500;
// Idle time after the last keystroke before the draft is saved server-side.
const DRAFT_SAVE_MS = 1000;
// Sticking distance: within this many px of the bottom counts as "at bottom".
const NEAR_BOTTOM_PX = 40;
// Outgoing images — the caps mirror the send endpoint (which answers 400 on
// violations), so a doomed selection fails here instead of after the upload.
const MAX_IMAGES_PER_SEND = 5;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
// Client-side downscale before upload, as the native clients do: longest edge
// to IMAGE_MAX_EDGE, re-encoded as JPEG at IMAGE_JPEG_QUALITY.
const IMAGE_MAX_EDGE = 1600;
const IMAGE_JPEG_QUALITY = 0.85;

// How long a cleared draft stays recoverable. Long enough to notice the box is
// empty and reach for the undo, short enough that the control is not still
// sitting there when the user comes back to a chat they left minutes ago —
// where it would offer to restore text they have long since abandoned.
const UNDO_CLEAR_MS = 12000;

// Quick patterns: canned companion prompts over the current draft/thread.
// A chip is nothing but a pre-filled companion turn (see the design doc) —
// the user still reads and sends it.
const QUICK_PATTERNS = [
  { id: 'proofread', label: 'Proofread',
    prompt: (draft) => draft
      ? `Proofread this draft — fix grammar and typos, keep my tone:\n\n${draft}`
      : 'Proofread my draft before I send it.' },
  { id: 'translate', label: 'Translate',
    prompt: () => 'Translate the last message for me, and draft a reply in the same language.' },
  { id: 'summarize', label: 'Summarize',
    prompt: () => 'Summarize this chat since my last reply.' },
];

// Minimal inline rendering for mirrored channel messages: escaped text,
// clickable http(s) links, hard line breaks — nothing more. The mirror shows
// what was sent, so no Markdown semantics are applied to other people's words
// (the companion pane, an agent conversation, does render Markdown).
const URL_RE = /\bhttps?:\/\/[^\s<]+/g;

function linkify(text) {
  const s = esc(text || '');
  return s.replace(URL_RE, (u) => {
    const m = /(&[a-z]+;|[.,;:!?)\]]+)$/.exec(u);
    const tail = m ? m[0] : '';
    const url = tail ? u.slice(0, -tail.length) : u;
    return `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>${tail}`;
  }).replace(/\n/g, '<br>');
}

function fmtBytes(n) {
  if (!(n > 0)) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

function fmtTime(iso) {
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? ''
    : t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function dayKey(iso) {
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? '' : t.toDateString();
}

// Strictly-after for ISO timestamps. The ledgers mix offsets (+02:00, Z), so
// epoch comparison is the truth; the string fallback only covers unparsable
// values.
function tsAfter(a, b) {
  const ta = Date.parse(a);
  const tb = Date.parse(b);
  if (!Number.isNaN(ta) && !Number.isNaN(tb)) return ta > tb;
  return String(a) > String(b);
}

function fmtDay(iso) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '';
  const now = new Date();
  const today = now.toDateString();
  const yesterday = new Date(now.getTime() - 86400000).toDateString();
  if (t.toDateString() === today) return 'Today';
  if (t.toDateString() === yesterday) return 'Yesterday';
  const opts = { weekday: 'short', day: 'numeric', month: 'short' };
  if (t.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
  return t.toLocaleDateString(undefined, opts);
}

class RetinueChatPage extends HTMLElement {
  constructor() {
    super();
    this._state = 'loading';   // loading | ok | error
    this._error = '';
    this._chat = null;         // ChatSummary (see contract)
    this._messages = [];       // channel messages, ascending
    this._companion = [];      // companion-thread messages, ascending
    this._companionId = '';    // its conversation id, once the thread exists
    this._compPending = false; // Ara's turn is running in the companion
    this._compStatus = '';     // the pending line the conversation reports
    this._compCreate = null;   // in-flight lazy creation, shared by callers
    // The companion's model picker, the same choice the conversations card
    // offers: the list comes from the gateway (single source of truth), ''
    // means the gateway default, and the choice is stored on the thread —
    // effective next turn, since each turn is a fresh `claude -p`. Before the
    // thread exists the choice is held here and pinned right after creation.
    this._models = [];
    this._compModel = '';
    // The last model the gateway confirmed for the thread (stored, or read
    // back from it). A refused pin falls back to this locally, so the picker
    // is honest even when the reconciling reload fails as well.
    this._compModelConfirmed = '';
    // An unpinned thread that Ara junior escalated stays with Ara senior
    // (the gateway keeps it on the frontier tier until the picker is
    // touched), so the picker must not show it as the default: it renders
    // a distinct "escalated" state instead, from which any choice — the
    // default included — is a change that clears the escalation.
    this._compEscalated = false;
    // Whether an existing thread's document has been read at least once.
    // Until then its model and escalation are unknown, and the picker stays
    // hidden rather than show the default for a thread that may be pinned
    // or escalated. A thread that does not exist yet needs no read.
    this._compLoaded = false;
    // Model fields are adopted only from responses that began after the
    // latest pin started: a poll that was already in flight when the user
    // switched carries the old model and must not overwrite the new one.
    this._pinGen = 0;
    // Pins are serialized through this promise chain, and a turn waits for
    // it: the gateway runs requests concurrently, so a model POST that is
    // merely in flight when the message POST arrives may not govern that
    // turn. It resolves to the latest pin's outcome — false aborts the turn
    // queued behind a refused pin — and is reset to true once that failure
    // has been handled, so one refusal does not block every later send.
    this._pinQueue = Promise.resolve(true);
    this._compTimer = null;
    this._pane = 'chat';       // chat | companion (phone pane indicator)
    this._draft = '';          // chat composer text
    this._draftByAra = false;  // composer holds the staged agent draft
    this._unconfirmed = [];    // sends the gateway never confirmed; see _sendChat
    this._undoing = false;     // a clear is being restored; hold draft saves
    this._undo = null;         // {text, byAra} a cleared draft, still recoverable
    this._undoTimer = null;
    this._compDraft = '';      // companion composer text
    this._localSeq = 0;        // ids for optimistic (not yet confirmed) bubbles
    this._outImages = [];      // staged composer images: {blob, url, content_type, width, height, name}
    this._imgError = '';       // staged-image error line (limit hit, unreadable file)
    this._lightbox = null;     // {url, alt} while the image overlay is open
    this._lbClosing = false;   // its history entry is being unwound
    this._lbKeydown = null;    // window keydown handler while the lightbox is open
    this._lbPrevFocus = null;  // element to restore focus to on lightbox close
    this._onPop = null;
    this._noteTimers = {};     // per-composer note timeouts (chat, companion)
    this._sizes = this._loadSizes();
    this._wide = matchMedia(WIDE_FRAME);
    // Live-API state: the chat's own messages URL (followed verbatim from the
    // summary), the ids already rendered (poll de-dup), the version the next
    // draft write is based on, the draft text the server last accepted, and
    // the newest ts already posted to the read watermark.
    this._msgUrl = '';
    this._seen = new Set();
    this._draftVersion = 0;
    this._draftSaved = '';
    this._draftTimer = null;
    this._draftInflight = null;
    this._pollTimer = null;
    this._lastReadPosted = '';
    this._onVis = null;
    // Voice dictation: this element is a host of voice.js's presentation
    // contract — the third one, after conversations.js and project-page.js.
    // The host owns the MediaRecorder state machine and what ✓/➤ mean;
    // voice.js owns the recording/status rows, the waveform and their styles.
    // One live recording at a time, targeted at one of the two composers.
    this._recState = 'idle';   // idle | recording
    this._recTarget = null;    // 'chat' | 'companion' while recording
    this._recChunks = [];
    this._mediaRecorder = null;
    this._recStream = null;
    this._recIntent = null;    // 'review' | 'send', decided by the ✓/➤ tap
    this._recAborted = false;  // recording was discarded via the abort button
    this._voiceJobs = {};      // per-target {sending, phase} while a dictation runs
    this._voiceErrors = {};    // per-target error line, shown by that composer
    this._wave = new Waveform(this);
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._id = new URLSearchParams(location.search).get('id') || '';
    // Where "back" leads. A same-origin referrer means the chat was opened
    // from inside the app (the dashboard card, the chats list, another chat),
    // so there is an entry to return to and the user expects the place they
    // came from — not a list they then have to scroll to escape. Opened cold
    // (a push notification, a bookmark, the home-screen icon) there is no such
    // entry, and the chats list is the honest landing place.
    this._fromApp = this._openedFromApp();
    // Pane arrangement differs across the breakpoint; re-render on a flip
    // (drafts survive — they live in fields, mirrored on every input event).
    this._onFrame = () => this.render();
    this._wide.addEventListener('change', this._onFrame);
    // The lightbox holds one history entry while open, so the platform back
    // gesture closes it instead of leaving the page; popstate is where the
    // actual dismissal happens, whichever way the entry is unwound.
    this._onPop = () => { if (this._lightbox) this._dismissLightbox(); };
    window.addEventListener('popstate', this._onPop);
    this.render();
    this._load();
  }

  disconnectedCallback() {
    this._wide.removeEventListener('change', this._onFrame);
    if (this._onPop) window.removeEventListener('popstate', this._onPop);
    this._onPop = null;
    if (this._lightbox) this._dismissLightbox();
    Object.values(this._noteTimers).forEach((t) => clearTimeout(t));
    this._noteTimers = {};
    if (this._draftTimer) clearTimeout(this._draftTimer);
    if (this._pollTimer) clearInterval(this._pollTimer);
    if (this._compTimer) clearTimeout(this._compTimer);
    this._compTimer = null;
    if (this._onVis) document.removeEventListener('visibilitychange', this._onVis);
    this._onVis = null;
    this._stopRecording();
    this._wave.stop();
    this._stopStream();
  }

  async _load() {
    if (!this._id) {
      this._state = 'error';
      this._error = 'No chat id in the address.';
      this.render();
      return;
    }
    // The offered model list is independent of the chat and never blocks it:
    // it lands whenever it lands and the picker appears then (or not at all).
    this._loadModels();
    try {
      // Two hops, both part of the contract: the list names each chat's
      // message document in `messages`, so the client never builds a message
      // URL itself. A 502 is the store's honest "unreachable" answer — show
      // it as such rather than a generic failure.
      const listRes = await fetch(LIST_URL, { cache: 'no-store' });
      if (listRes.status === 502) throw new Error('store');
      if (!listRes.ok) throw new Error(String(listRes.status));
      const list = await listRes.json();
      const summary = (list.chats || []).find((c) => c.id === this._id);
      if (!summary || !summary.messages) {
        this._state = 'error';
        this._error = 'This chat is not in the chat list.';
        this.render();
        return;
      }
      this._msgUrl = summary.messages;
      const msgRes = await fetch(this._msgUrl, { cache: 'no-store' });
      if (msgRes.status === 502) throw new Error('store');
      if (!msgRes.ok) throw new Error(String(msgRes.status));
      const doc = await msgRes.json();
      this._chat = doc.chat || summary;
      this._messages = Array.isArray(doc.messages) ? doc.messages : [];
      // A chat that has been discussed before names its companion thread; one
      // that has not carries null, and nothing is created until the user
      // actually says something (see _ensureCompanion).
      this._companionId = this._chat.companion || '';
      this._compLoaded = !this._companionId;
      this._seen = new Set(this._messages.map((m) => m.id));
      // Pin the unread waterline to where it was when the chat opened —
      // messages sent from here are appended below it, and a re-render must
      // not drift the line (messenger convention: it stays put while the
      // thread is open).
      this._firstUnread = this._chat.unread
        ? this._messages.length - this._chat.unread : -1;
      // A staged draft lands in the composer, marked with its author — the
      // agent writes into the draft, the user's send button sends. The draft
      // version anchors all later version-guarded saves.
      const draft = this._chat.draft;
      this._draftVersion = (draft && draft.version) || 0;
      this._draftSaved = (draft && draft.text) || '';
      if (draft && draft.text && !this._draft) {
        this._draft = draft.text;
        this._draftByAra = draft.author === 'agent';
      }
      this._state = 'ok';
      try { document.title = `Retinue — ${this._chat.name}`; } catch (_e) { /* ignore */ }
      this.render();
      // Opening the chat reads it: advance the watermark to the newest
      // message, then keep the mirror fresh on the conversations cadence.
      this._postRead(this._newestTs());
      this._startPolling();
      // An existing companion is read straight away; a chat with none stays
      // empty until the first turn creates one.
      if (this._companionId) {
        this._loadCompanion();
        this._scheduleCompanionPoll();
      }
    } catch (err) {
      this._state = 'error';
      this._error = err && err.message === 'store'
        ? 'The message store is unreachable right now — try again in a moment.'
        : 'Could not load this chat. Offline?';
      this.render();
    }
  }

  _newestTs() {
    const m = this._messages[this._messages.length - 1];
    return m ? m.ts : '';
  }

  _atBottom() {
    const t = this.shadowRoot.querySelector('[data-chat-thread]');
    return !!t && t.scrollHeight - t.scrollTop - t.clientHeight < NEAR_BOTTOM_PX;
  }

  // ── Live mirror: poll, read watermark ──────────────────────────────────────
  _startPolling() {
    if (this._pollTimer) return;
    this._pollTimer = setInterval(() => this._poll(), POLL_MS);
    this._onVis = () => {
      if (document.hidden) return;
      // Coming back to a visible page: catch up at once, and mark the newest
      // message read if the user is looking at the bottom of the thread.
      this._poll();
      this._loadCompanion();
      if (this._atBottom()) this._postRead(this._newestTs());
    };
    document.addEventListener('visibilitychange', this._onVis);
  }

  async _poll() {
    if (this._state !== 'ok' || !this._msgUrl || document.hidden) return;
    try {
      const res = await fetch(this._msgUrl, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const doc = await res.json();
      const msgs = Array.isArray(doc.messages) ? doc.messages : [];
      const stick = this._atBottom();
      let newest = '';
      // Append only unseen messages — never a full re-render, which would
      // fight the composer (focus, IME, dictation) and the scroll position.
      // Chat streams are monotonic enough that appending in payload order is
      // right; anything older is history this page already shows.
      for (const m of msgs) {
        if (!m || this._seen.has(m.id)) continue;
        this._seen.add(m.id);
        if (this._reconcileUnconfirmed(m)) {
          if (!newest || tsAfter(m.ts, newest)) newest = m.ts;
          continue;
        }
        this._messages.push(m);
        this._appendChatMessage(m, stick);
        if (!newest || tsAfter(m.ts, newest)) newest = m.ts;
      }
      if (newest && stick) this._postRead(newest);
      // Ara answers a companion turn by staging a reply here. Adopt it only
      // into an EMPTY composer — never over what the user is typing — and
      // only the composer block: a full render would rebuild the mirror and
      // the companion pane under a user who is reading or typing in them.
      const d = doc.chat && doc.chat.draft;
      if (d && d.text && !this._draft && d.version !== this._draftVersion) {
        this._draftVersion = d.version;
        this._draftSaved = d.text;
        this._draft = d.text;
        this._draftByAra = d.author === 'agent';
        // A draft arriving is a better offer than the one the user just threw
        // away; restoring the old text over it would be the wrong undo.
        this._setUndo(null);
        this._refreshChatComposer();
      }
    } catch (_err) {
      // Offline or store blip: keep the last rendered state; the next poll
      // reconciles.
    }
  }

  // One arriving message against the sends whose identity we never learned.
  //
  // A send the gateway did not confirm in time came back without a message id
  // and with a timestamp of the server's own making, so nothing about it will
  // match the record the channel eventually writes. Left alone, the poll would
  // append that record beside the bubble already on screen and the user would
  // see their own message twice — for as long as the page stayed open, since
  // the client's dedup is by id and both ids are real to it.
  //
  // So an unconfirmed send is reconciled by its words: the first outbound
  // record carrying the same text, at or after the moment we stopped waiting,
  // is that send, and it replaces the bubble in place rather than joining it.
  // Oldest record first, so two identical sends resolve to two bubbles in the
  // order they were made. Returns true when the message was absorbed.
  _reconcileUnconfirmed(m) {
    if (!this._unconfirmed.length || !m || m.direction !== 'out') return false;
    const text = m.text || '';
    const i = this._unconfirmed.findIndex(
      (u) => u.text === text && (!u.since || !tsAfter(u.since, m.ts)));
    if (i < 0) return false;
    const [pending] = this._unconfirmed.splice(i, 1);
    const idx = this._messages.findIndex((x) => x && x.id === pending.id);
    if (idx >= 0) this._messages[idx] = m;
    // Found by walking rather than by selector: a synthesised id carries the
    // chat id, so it holds ':', '#' and '+', and building a selector out of it
    // would need escaping this has no reason to get wrong.
    const thread = this.shadowRoot.querySelector('[data-chat-thread]');
    const node = thread && [...thread.querySelectorAll('[data-mid]')]
      .find((n) => n.getAttribute('data-mid') === pending.id);
    if (node) {
      const tpl = document.createElement('template');
      tpl.innerHTML = this._chatMsgHtml(m);
      node.replaceWith(tpl.content.firstElementChild);
    } else if (idx < 0) {
      // The bubble is gone (a re-render dropped it) and nothing holds the
      // message: let the caller append it normally rather than losing it.
      return false;
    }
    return true;
  }

  // Advance the server-side read watermark, monotonically: the newest ts is
  // posted once, and a failure returns it to the pool for the next occasion.
  async _postRead(ts) {
    if (!ts || (this._lastReadPosted && !tsAfter(ts, this._lastReadPosted))) return;
    const prev = this._lastReadPosted;
    this._lastReadPosted = ts;
    try {
      const res = await fetch(`/chats/${encodeURIComponent(this._id)}/read`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ts }),
      });
      if (!res.ok) throw new Error(String(res.status));
    } catch (_err) {
      this._lastReadPosted = prev;
    }
  }

  // ── Shared draft: debounced, version-guarded saves ─────────────────────────
  _scheduleDraftSave() {
    if (this._draftTimer) clearTimeout(this._draftTimer);
    this._draftTimer = setTimeout(() => this._saveDraft(), DRAFT_SAVE_MS);
  }

  // The in-flight save is tracked so a send can wait for it: tapping Send
  // blurs the field, which fires an immediate save — un-ordered, that write
  // could land after the send's server-side draft clear and resurrect the
  // just-sent text as a draft.
  _saveDraft(retried) {
    const p = this._saveDraftNow(retried);
    this._draftInflight = p;
    p.finally(() => { if (this._draftInflight === p) this._draftInflight = null; });
    return p;
  }

  async _saveDraftNow(retried) {
    if (this._draftTimer) clearTimeout(this._draftTimer);
    this._draftTimer = null;
    // A restore is in flight: the server is about to say what the draft is,
    // author included. Writing here would race it, and would write the wrong
    // author besides — this endpoint can only ever stamp "user".
    if (this._undoing) return;
    const text = this._draft;
    if (text === this._draftSaved) return;
    try {
      const res = await fetch(`/chats/${encodeURIComponent(this._id)}/draft`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, version: this._draftVersion }),
      });
      const data = await res.json();
      if (res.status === 409) {
        // Someone else moved the draft. Adopt the server's version — and its
        // text, when it actually has different words — instead of clobbering.
        this._draftVersion = data.version || 0;
        const server = (data.draft && data.draft.text) || '';
        if (!server && text.trim() && !retried) {
          // A bare version bump (e.g. the counter advanced past a cleared
          // draft): our words are not in conflict with anything — retry once
          // on the adopted version.
          await this._saveDraft(true);
          return;
        }
        if (server !== this._draft) {
          this._draft = server;
          this._draftSaved = server;
          this._draftByAra = !!(data.draft && data.draft.author === 'agent');
          this.render();
          this._showNote('Draft updated elsewhere.');
        } else {
          this._draftSaved = server;
        }
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      this._draftVersion = data.version || this._draftVersion;
      this._draftSaved = text;
    } catch (_err) {
      // Offline: the draft stays local; the next edit (or blur) retries.
    }
  }

  // ── Splitter persistence (wide layout) ─────────────────────────────────────
  _loadSizes() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY)) || {}; }
    catch (_e) { return {}; }
  }

  _saveSizes() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(this._sizes)); }
    catch (_e) { /* private mode: resizing still works for the session */ }
  }

  _applySizes() {
    const panes = this.shadowRoot.querySelector('[data-panes]');
    if (!panes) return;
    if (typeof this._sizes.comp === 'number') {
      panes.style.setProperty('--comp-w', `${this._sizes.comp}px`);
    } else {
      panes.style.removeProperty('--comp-w');
    }
  }

  _clampComp(px) {
    const panes = this.shadowRoot.querySelector('[data-panes]');
    const max = panes ? panes.getBoundingClientRect().width * MAX_COMP_FRACTION : 800;
    return Math.min(Math.max(px, MIN_COMP_PX), max);
  }

  // ── Rendering ──────────────────────────────────────────────────────────────
  render() {
    let body;
    if (this._state === 'loading') {
      body = '<div class="center muted">&#8230;</div>';
    } else if (this._state === 'error') {
      body = `<div class="center muted"><p>${esc(this._error)}</p>` +
        `<p><a class="backlink" href="${CHATS_URL}">&#8249; All chats</a></p></div>`;
    } else {
      body = this._headHtml() + this._panesHtml();
    }
    this.shadowRoot.innerHTML = `<style>${CSS}${VOICE_CSS}${MD_CSS}</style>` +
      `<section class="page">${body}</section>`;
    if (this._state === 'ok') {
      this._applySizes();
      this._wire();
      this._scrollThread('[data-chat-thread]');
      this._scrollThread('[data-comp-thread]');
      this._setPane(this._pane, 'instant');
    }
    // A full render replaces the shadow DOM wholesale; an open lightbox (its
    // node lives beside .page) survives by being re-appended.
    if (this._lightbox) this._renderLightbox();
  }

  _headHtml() {
    const c = this._chat;
    const ch = esc((CHANNELS[c.channel] || { label: c.channel }).label);
    // The peer as the summary names them. Not sliced out of the id any more:
    // an id can carry an account segment ahead of the key, and slicing at the
    // first colon would put that in the header as the correspondent.
    const key = c.key || c.id.slice(c.id.indexOf(':') + 1);
    // Which account this conversation is on, where the chat says. The header is
    // where it belongs: two accounts talking to one peer are two chats with the
    // same name, and this is the line that says which one is open.
    // This line has room for one fact, and where the chat knows its account
    // that fact is the account: two accounts talking to one peer are two chats
    // with the same title and the same avatar, and nothing else on the page
    // says which of them is open. So it displaces the peer's handle (the title
    // already names the peer), the group marker and member count, and even the
    // channel — which the avatar's own badge carries anyway. The header gives
    // this line about 140px next to the back button, avatar and pane tabs, and
    // any pairing of those facts overflows it; what a truncated subtitle drops
    // is the tail, which is exactly the half that disambiguates. Hence one
    // rule for both group and 1:1, short enough to always fit.
    const sub = c.account
      ? `via ${esc(c.account)}`
      : (c.group
        ? `${ch} group${c.members ? ` &middot; ${Number(c.members)} members` : ''}`
        : `${ch} &middot; ${esc(key)}`);
    return `<header class="chat-head">` +
      `<a class="back" href="${CHATS_URL}" data-back title="Back" aria-label="Back">&#8249;</a>` +
      avatarHtml(c) +
      `<div class="head-txt"><div class="head-name">${esc(c.name)}</div>` +
      `<small class="head-sub">${sub}</small></div>` +
      `<nav class="pane-tabs" role="tablist" aria-label="Pane">` +
      `<button role="tab" data-pane-tab="chat" aria-selected="true">Chat</button>` +
      `<button role="tab" data-pane-tab="companion" aria-selected="false">Ara</button>` +
      `</nav></header>`;
  }

  _panesHtml() {
    return `<div class="panes" data-panes>` +
      `<section class="pane pane-chat" aria-label="Chat">` +
      `<div class="thread" data-chat-thread>${this._chatThreadHtml()}</div>` +
      this._chatComposerHtml() +
      `</section>` +
      `<div class="pane-splitter" data-splitter role="separator" aria-orientation="vertical" ` +
      `aria-label="Resize companion pane" tabindex="0" ` +
      `title="Drag to resize &middot; double-click to reset"></div>` +
      `<section class="pane pane-companion" aria-label="Ara">` +
      `<div class="comp-bar" data-comp-bar><span class="comp-who">Ara</span>` +
      `<span class="comp-hint">reads this chat, writes into your draft</span>` +
      `<span class="comp-actions" data-comp-picker>${this._modelPickerHtml()}</span></div>` +
      `<div class="thread" data-comp-thread>${this._companionThreadHtml()}</div>` +
      this._companionComposerHtml() +
      `</section></div>`;
  }

  // The mirror: day separators, an unread waterline, bubbles left (inbound) /
  // right (outbound) with the author on every outbound bubble — the user, Ara,
  // or the user's own phone — a distinction the real clients cannot show.
  _chatThreadHtml() {
    const msgs = this._messages;
    if (!msgs.length) return '<div class="center muted">No messages.</div>';
    const unread = this._chat.unread || 0;
    let html = '';
    let lastDay = '';
    msgs.forEach((m, i) => {
      const day = dayKey(m.ts);
      if (day !== lastDay) {
        html += `<div class="day-sep"><span>${esc(fmtDay(m.ts))}</span></div>`;
        lastDay = day;
      }
      if (i === this._firstUnread) {
        html += `<div class="unread-sep"><span>${unread} unread</span></div>`;
      }
      html += this._chatMsgHtml(m);
    });
    return html;
  }

  _chatMsgHtml(m) {
    const out = m.direction === 'out';
    const author = out ? (m.author || 'user') : '';
    const cls = out ? `msg out by-${author}` : 'msg in';
    let head = '';
    if (out) {
      const tag = author === 'agent' ? esc(m.agent || 'Ara')
        : author === 'device' ? 'You &middot; phone' : 'You';
      head = `<div class="msg-head"><small class="who">${tag}</small></div>`;
    } else if (this._chat.group && m.sender_name) {
      head = `<div class="msg-head"><small class="who sender" ` +
        `style="color:${colorFor(m.sender)}">${esc(m.sender_name)}</small></div>`;
    }
    const media = (m.attachments || []).map((a) => this._attachmentHtml(a)).join('');
    const text = m.text ? `<span class="txt">${linkify(m.text)}</span>` : '';
    return `<div class="${cls}" data-mid="${esc(m.id)}">${head}<div class="bubble">${media}${text}` +
      `<span class="stamp">${esc(fmtTime(m.ts))}</span></div></div>`;
  }

  // One attachment inside a bubble, by type. Loading media must never shift
  // the thread's scroll position: when the record carries the intrinsic size
  // (sniffed at ingest; older records may lack it) the true box is reserved
  // up front — the inline aspect-ratio/width below — otherwise a fixed
  // placeholder frame holds the space. Images open the lightbox; a voice
  // note's player sits above the transcript, which is already the message
  // text; anything that is neither image, audio nor video is a file row —
  // the name and size the gateway stated — that opens the file itself, as
  // the native clients do.
  _attachmentHtml(a) {
    const type = String(a.type || '');
    const w = Number(a.width);
    const h = Number(a.height);
    const hasDims = w > 0 && h > 0;
    const dims = hasDims ? ` width="${w}" height="${h}"` : '';
    // The inline size pins BOTH the reserved box and the element's intrinsic
    // contribution: with bare width/height attributes, a shrink-to-fit bubble
    // measures the full attribute width during intrinsic sizing (a
    // percentage-bearing max-width is ignored there), stretching the bubble
    // far past the rendered medium. Fixed lengths only, so nothing is
    // cyclic: the box is min(cap, natural) — never an upscale — the ratio is
    // held by aspect-ratio through the load, and a narrower bubble clamps
    // via max-width:100% with the height following the ratio.
    const sized = (cap) => (hasDims
      ? ` style="aspect-ratio: ${w} / ${h}; width: min(${cap}px, ${w}px)"` : '');
    if (type.startsWith('image/')) {
      return `<button type="button" class="att-imgbtn" data-lightbox="${esc(a.url)}" ` +
        `data-alt="${esc(a.name || 'Image')}" aria-label="View image full screen">` +
        `<img class="att-img${hasDims ? '' : ' no-dims'}" src="${esc(a.url)}" ` +
        `alt="${esc(a.name || 'image')}"${dims}${sized(280)} loading="lazy"></button>`;
    }
    if (type.startsWith('audio/')) {
      return `<audio class="att-audio" controls preload="none" src="${esc(a.url)}" ` +
        `title="${esc(a.name || 'Voice message')}"></audio>`;
    }
    if (type.startsWith('video/')) {
      return `<video class="att-video${hasDims ? '' : ' no-dims'}" controls preload="metadata" ` +
        `playsinline src="${esc(a.url)}"${dims}${sized(280)}></video>`;
    }
    const size = fmtBytes(Number(a.size));
    return `<a class="att-file" href="${esc(a.url)}" target="_blank" rel="noopener" ` +
      `title="${esc(a.name || 'Attachment')}">&#128206; <span class="att-name">` +
      `${esc(a.name || 'Attachment')}</span>${size ? ` <span class="att-size">${esc(size)}</span>` : ''}</a>`;
  }

  // ── Leaving the chat ───────────────────────────────────────────────────────
  _openedFromApp() {
    try {
      const ref = document.referrer;
      return !!ref && new URL(ref, location.href).origin === location.origin;
    } catch (_e) {
      return false;
    }
  }

  // An installed PWA has no browser chrome and no back gesture on every
  // platform, so this control is the way out of a chat, not a convenience.
  // The lightbox parks one history entry while it is open and unwinds it on
  // close, so by the time this runs the top of the stack is the chat's own
  // entry again and one step back leaves the chat — but a press while the
  // overlay is still up closes that first, never two views at once.
  _goBack() {
    if (this._lightbox) { this._closeLightbox(); return; }
    if (this._fromApp && history.length > 1) { history.back(); return; }
    location.href = CHATS_URL;
  }

  // ── Lightbox ───────────────────────────────────────────────────────────────
  // A tapped image opens full screen on a dark scrim at natural fit, over the
  // same proxied URL (it is the original). Closing: tap, Esc, or the platform
  // back gesture/button — opening pushes one history entry, so back closes
  // the overlay instead of leaving the page (the conversations hash-view
  // precedent for view state on the history stack).
  _openLightbox(url, alt) {
    if (this._lightbox || !url) return;
    this._lightbox = { url, alt: alt || 'Image' };
    this._lbPrevFocus = this.shadowRoot.activeElement || null;
    history.pushState({ lightbox: true }, '', location.href);
    this._renderLightbox();
  }

  // User intent to close: unwind our history entry; the popstate handler does
  // the actual dismissal — the same path the back gesture takes. The unwind is
  // asynchronous, so the in-flight flag keeps a second press (the ✕ then the
  // header's back, in quick succession) from popping a second entry and
  // dropping the user out of the chat with it.
  _closeLightbox() {
    if (!this._lightbox || this._lbClosing) return;
    this._lbClosing = true;
    history.back();
  }

  _renderLightbox() {
    const root = this.shadowRoot;
    if (root.querySelector('.lightbox')) return;
    const { url, alt } = this._lightbox;
    const node = document.createElement('div');
    node.className = 'lightbox';
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-modal', 'true');
    node.setAttribute('aria-label', alt);
    node.tabIndex = -1;
    node.innerHTML = `<img class="lb-img" src="${esc(url)}" alt="${esc(alt)}">` +
      `<button type="button" class="lb-close" aria-label="Close">&#10005;</button>`;
    // One tap anywhere — scrim, image or the ✕ — closes.
    node.addEventListener('click', () => this._closeLightbox());
    root.appendChild(node);
    if (!this._lbKeydown) {
      this._lbKeydown = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); this._closeLightbox(); }
      };
      window.addEventListener('keydown', this._lbKeydown);
    }
    try { node.focus(); } catch (_e) { /* ignore */ }
  }

  _dismissLightbox() {
    this._lightbox = null;
    this._lbClosing = false;
    if (this._lbKeydown) {
      window.removeEventListener('keydown', this._lbKeydown);
      this._lbKeydown = null;
    }
    const node = this.shadowRoot && this.shadowRoot.querySelector('.lightbox');
    if (node) node.remove();
    if (this._lbPrevFocus && this._lbPrevFocus.isConnected) {
      try { this._lbPrevFocus.focus(); } catch (_e) { /* ignore */ }
    }
    this._lbPrevFocus = null;
  }

  _chatComposerHtml() {
    const draftTag = this._draftByAra
      ? `<div class="draft-tag" data-draft-tag ` +
        `title="Ara staged this text into the shared draft. Edit freely — sending sends your words.">` +
        `&#9998; Draft by Ara</div>`
      : '';
    const chips = QUICK_PATTERNS.map((p) =>
      `<button type="button" class="qchip" data-quick="${p.id}">${esc(p.label)}</button>`
    ).join('');
    return `<div class="composer">` +
      `<div class="chips" role="toolbar" aria-label="Quick patterns">${chips}</div>` +
      `<div class="note" data-note role="status" hidden></div>` +
      draftTag +
      this._imgPreviewsHtml() +
      this._errRowHtml('chat') +
      this._composerRowHtml('chat', 'Message …', true) +
      `</div>`;
  }

  _errRowHtml(target) {
    const err = this._voiceErrors[target] || (target === 'chat' ? this._imgError : '');
    return err ? `<div class="attach-err" role="status">${esc(err)}</div>` : '';
  }

  // Staged outgoing images: small thumbnails above the input row, each with
  // its own remove — they ride along until the send (or their ✕) takes them.
  _imgPreviewsHtml() {
    if (!this._outImages.length) return '';
    const items = this._outImages.map((im, i) =>
      `<span class="imgp"><img src="${esc(im.url)}" alt="${esc(im.name || 'image')}">` +
      `<button type="button" class="imgp-x" data-rmimg="${i}" ` +
      `aria-label="Remove image">&#10005;</button></span>`).join('');
    return `<div class="img-previews" data-previews>${items}</div>`;
  }

  // The paperclip, tucked INSIDE the text field exactly as the conversations
  // composer tucks its own: a label over a hidden file input, images only since
  // that is what the send endpoint carries. Inside rather than beside, because
  // a round control in the row costs the field 46px of a phone's width while
  // one inside costs only padding — and because the two composers should not
  // teach two different places to look for the same affordance.
  //
  // One control, not two: the phone's file chooser already offers the camera
  // among its sources, so a separate camera button buys a shortcut at the price
  // of a whole control's width.
  _clipHtml() {
    return `<label class="clip" title="Attach images" aria-label="Attach images">` +
      `<input type="file" hidden multiple accept="image/*" data-attach>` +
      `<span aria-hidden="true">&#128206;</span></label>`;
  }

  // One composer's input row — or, while this target records or transcribes,
  // the shared recording/status row from voice.js in its place, so the text
  // field (and the phone keyboard) never reappears mid-flow: the same
  // behaviour as the conversation composer and the project command bar.
  _composerRowHtml(target, placeholder, withClear) {
    if (this._recState === 'recording' && this._recTarget === target) {
      return recordingRowHtml();
    }
    const job = this._voiceJobs[target];
    if (job) {
      const label = job.phase === 'sending' ? 'Sending …'
        : (job.sending ? 'Transcribing & sending …' : 'Transcribing …');
      return statusRowHtml(label);
    }
    const isChat = target === 'chat';
    const value = isChat ? this._draft : this._compDraft;
    const micBtn = canRecord()
      ? `<button type="button" class="mic" data-mic="${target}" ` +
        `title="Record a voice message" aria-label="Record a voice message">&#127908;</button>`
      : '';
    const sendBtn = `<button type="submit" class="send" title="Send" aria-label="Send">&#10148;</button>`;
    // The clear ✕ lives INSIDE the field, docked to its top-right corner: on
    // the send side, as the design asks, but within the field's own boundary,
    // so it reads — and taps — as part of the text box, visually and
    // physically distinct from the round send button beside it and never one
    // fat finger away from it. As a row button it would cost the empty field
    // width it does not need: it exists only while there is text to clear
    // (.has-text shows it and pads the text out from under it).
    // The ✕ and its undo share one slot in the field, and CSS picks between
    // them: the ✕ while there is text, the undo for a short while after a
    // clear. Both are rendered up front so the swap is a class toggle rather
    // than a rebuild — a rebuild here would cost the field its focus and the
    // phone its keyboard.
    const clearBtn = withClear
      ? `<button type="button" class="clear-inline" data-clear ` +
        `title="Clear message" aria-label="Clear message">&#10005;</button>` +
        `<button type="button" class="undo-inline" data-undo ` +
        `title="Undo clear" aria-label="Undo clear">&#8630;</button>`
      : '';
    // Both composers are the dashboard conversation composer's row: mic on the
    // left, send on the right, and what belongs inside the field lives inside
    // it. Keeping the two round controls in their fixed places is the point —
    // the mic is where the mic always is, whatever the field holds, so
    // dictating into an existing draft is just pressing it. The chat field
    // additionally carries the clear ✕ and the paperclip; the companion's
    // carries neither (it is Ara's own thread, and it never fights for width).
    const inField = isChat ? clearBtn + this._clipHtml() : clearBtn;
    const fieldCls = `field${withClear ? ' has-clear' : ''}` +
      `${isChat ? ' has-clip' : ''}${value ? ' has-text' : ''}` +
      `${this._undo ? ' has-undo' : ''}`;
    const field = `<div class="${fieldCls}" data-field>` +
      `<textarea rows="1" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}" ` +
      `autocomplete="off">${esc(value)}</textarea>` + inField + `</div>`;
    return `<form class="row" data-composer="${target}">` +
      micBtn + field + sendBtn + `</form>`;
  }

  // Companion messages reuse the conversation thread's visual language (same
  // bubble geometry and styles as conversations.js, Markdown via the shared
  // renderer) so this pane and real dashboard conversations render identically
  // by construction.
  _companionThreadHtml() {
    if (!this._companion.length && !this._compPending) {
      return `<div class="center muted comp-empty"><span class="e-ico" aria-hidden="true">&#x1F4AC;</span>` +
        `<p>Ask Ara about this chat &mdash; she reads it and stages replies into your draft.</p></div>`;
    }
    return this._companion.map((m) => this._companionMsgHtml(m)).join('')
      + this._compPendingHtml();
  }

  _companionMsgHtml(m) {
    const me = m.role === 'user';
    // The conversations card's role vocabulary: a relayed message carries the
    // acting agent's own name, everything else is Ara herself.
    const who = me ? 'You' : (m.agent || (m.role === 'agent' ? 'Retinue' : 'Ara'));
    return `<div class="cmsg${me ? ' me' : ''}">` +
      `<div class="cmsg-head"><small class="who">${esc(who)}</small>` +
      this._compMetaHtml(m) + `</div>` +
      `<div class="cbubble">${renderMarkdown(m.text)}` +
      this._compAttachHtml(m) + `</div></div>`;
  }

  // The header meta after the sender name. The companion thread is an ordinary
  // dashboard conversation, so its answers carry the same two byproducts the
  // conversations card already shows — which model answered, and that turn's
  // list-price cost — and a pane that hides them makes the model choice in this
  // very page unverifiable. Same vocabulary and same order as
  // conversations.js's _metaHtml (model · ~$cost · time); the time stays this
  // pane's clock time rather than that card's relative age, because the mirror
  // beside it is stamped in clock time and the two are read together. Each
  // piece is optional: a user turn has no model, and messages predating the
  // metadata simply omit what they lack.
  _compMetaHtml(m) {
    const bits = [];
    if (m.model_name) bits.push(`<span class="m-model">${esc(m.model_name)}</span>`);
    if (typeof m.cost_usd === 'number' && isFinite(m.cost_usd)) {
      bits.push(`<span class="m-cost" title="Approximate list-price cost — not the subscription bill">` +
        `~$${this._fmtCost(m.cost_usd)}</span>`);
    }
    const t = fmtTime(m.ts);
    if (t) bits.push(`<time datetime="${esc(m.ts || '')}">${esc(t)}</time>`);
    if (!bits.length) return '';
    return `<small class="cmeta">${bits.join('<span class="m-sep">·</span>')}</small>`;
  }

  // Cost with enough precision to stay meaningful for cheap turns: sub-cent
  // values get more decimals so they don't collapse to "~$0.00". Mirrors
  // conversations.js so the same turn reads identically in both surfaces.
  _fmtCost(v) {
    const c = Math.abs(v);
    if (c === 0) return '0';
    if (c < 0.01) return c.toFixed(4);
    if (c < 1) return c.toFixed(3);
    return c.toFixed(2);
  }

  // A companion message can carry files like any conversation message; they
  // are served by the thread's own attachment endpoint, behind the dashboard's
  // auth. A plain row, not the conversations card's view/save pair — reading a
  // PDF belongs in the thread on the dashboard, not in this side pane.
  _compAttachHtml(m) {
    const atts = Array.isArray(m.attachments) ? m.attachments : [];
    if (!atts.length || !this._companionId) return '';
    return atts.map((a) => {
      const url = `/conversations/${encodeURIComponent(this._companionId)}` +
        `/attachments/${encodeURIComponent(a.id)}`;
      const name = a.filename || 'attachment';
      return `<a class="cattach" href="${esc(url)}" download="${esc(name)}">` +
        `<span aria-hidden="true">&#128206;</span>${esc(name)}</a>`;
    }).join('');
  }

  // Ara's turn, while it runs: the conversation API's `pending` flag is the
  // signal, and it stays on the thread (not on the composer) so it reads as
  // the answer being written.
  _compPendingHtml() {
    if (!this._compPending) return '';
    const label = this._compStatus || 'Ara is working on this';
    return `<div class="cmsg" data-comp-pending>` +
      `<div class="cmsg-head"><small class="who">Ara</small></div>` +
      `<div class="cbubble cpending" role="status">` +
      `<span class="cdots" aria-hidden="true"><i></i><i></i><i></i></span>` +
      `<span>${esc(label)} &#8230;</span></div></div>`;
  }

  // The model dropdown, the compact form of the conversations card's (gear +
  // select; see _modelPickerHtml there for the rules it follows). Governs
  // Ara's own turn in the companion thread only — dispatched subagents keep
  // their own models. Hidden unless the gateway offers more than one model.
  // '' (the gateway default) shows as the entry the list flags `default`;
  // only when no entry is flagged does a hidden placeholder keep the select
  // from claiming a concrete model the thread is not running.
  _modelPickerHtml() {
    const models = this._models || [];
    if (models.length < 2 || !this._compLoaded) return '';
    let sel = this._compModel || '';
    const escalated = !sel && this._compEscalated;
    if (!escalated && !models.some((m) => m.id === sel)) {
      const def = models.find((m) => m.default);
      sel = def ? def.id : '';
    }
    // The escalated state is a hidden, unpickable row: it names the tier
    // rather than a model (the frontier model need not be on the offered
    // list at all), and leaves every real entry — the default one too —
    // a change away, which is what clears the escalation.
    const placeholder = escalated
      ? '<option value="" hidden selected>Ara senior (escalated)</option>'
      : (models.some((m) => m.id === sel) ? ''
        : '<option value="" hidden selected>Default</option>');
    const opts = placeholder + models.map((m) =>
      `<option value="${esc(m.id)}"${m.id === sel ? ' selected' : ''}>` +
      `${esc(m.label)}</option>`).join('');
    const title = 'Model for Ara’s replies in this chat’s thread. ' +
      'Dispatched subagents (Secretary, …) keep their own models.';
    return `<label class="model-pick" title="${title}">` +
      `<span class="mp-ico" aria-hidden="true">⚙</span>` +
      `<select data-model aria-label="${title}">${opts}</select></label>`;
  }

  // Fetch the offered model list once. A failure (or a single-model list)
  // simply leaves the picker hidden — the pane works exactly as before.
  async _loadModels() {
    try {
      const res = await fetch('/conversation-models', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      if (Array.isArray(data.models)) {
        this._models = data.models;
        this._syncPicker();
      }
    } catch (_err) { /* picker stays hidden */ }
  }

  // Bring the picker in line with the state — in place, never a full render
  // (which would rebuild the mirror and the chat composer). Rebuilt wholesale
  // when the list or the choice changed under it; left alone while the user
  // has it open, so a poll cannot snap a half-made choice away — unless
  // `force`, for a choice the gateway just refused: what the select shows is
  // then known to be wrong, focus or not.
  _syncPicker(force = false) {
    const host = this.shadowRoot.querySelector('[data-comp-picker]');
    if (!host) return;
    const sel = host.querySelector('[data-model]');
    if (!force && sel && this.shadowRoot.activeElement === sel) return;
    const html = this._modelPickerHtml();
    if (host.innerHTML !== html) host.innerHTML = html;
  }

  // The dropdown changed. An existing thread has it persisted server-side
  // right away (effective next turn, and a reload keeps it); a thread not yet
  // created holds the choice until _ensureCompanion pins it. The gateway
  // treats a picker touch as the user taking manual control of the tier, so
  // it also ends a standing escalation to Ara senior. A pin the gateway did
  // not take is said so, and the picker goes back to what the thread really
  // runs on — it must never show a model the next turn will not use.
  async _onModelChange(value) {
    const model = value || '';
    this._compModel = model;
    if (!this._companionId) return;
    await this._pinAndReport(this._companionId, model);
    await this._loadCompanion();
  }

  // Pin, record the outcome, and report a refusal: on success the model is
  // the confirmed one; on refusal the picker falls back to the confirmed
  // model locally and says so — but only while the refused value is still
  // the current choice. A refusal for a choice the user has since replaced
  // is not reported at all: the newer pin is queued and reports for itself.
  async _pinAndReport(cid, model) {
    const ok = await this._pinModel(cid, model);
    if (ok) {
      this._compModelConfirmed = model;
    } else if (this._compModel === model) {
      this._compModel = this._compModelConfirmed;
      this._syncPicker(true);
      this._showNote("Couldn't switch the model &mdash; the thread keeps the one it had.",
        'companion');
    }
    return ok;
  }

  // Wait until no pin is queued or in flight, and say how the last one went
  // (true when there was none). A pin that starts while waiting is waited
  // for too, and a refusal counts only if nothing newer replaced it — so a
  // turn behind a refused pin is aborted, while one behind a refused pick
  // that the user already corrected goes out on the corrected model.
  async _settledPins() {
    for (;;) {
      const gen = this._pinGen;
      const ok = await this._pinQueue;
      if (this._pinGen !== gen) continue;
      return ok;
    }
  }

  // Persist a model choice; resolves true when the gateway stored it, false
  // on an HTTP error or a dropped connection — never throws. Queued behind
  // any earlier pin (rapid re-selections land in order) and ahead of the
  // next companion turn: _pinQueue resolves to this pin's outcome while it
  // is the latest, and a refusal is cleared from it once reported here so
  // only the turn already waiting on it is aborted.
  _pinModel(cid, model) {
    // From here on, a response that began earlier no longer speaks for the
    // thread's model (see _pinGen).
    this._pinGen += 1;
    const run = this._pinQueue.then(async () => {
      try {
        const res = await fetch(`/conversations/${encodeURIComponent(cid)}/model`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        return res.ok;
      } catch (_err) {
        return false;
      }
    });
    // One promise for both the caller and the queue, so their continuations
    // run in registration order: the picker's own report (registered at
    // selection time) first, a turn queued behind it after — its note is the
    // one left standing.
    const queued = run.catch(() => false).then((ok) => {
      if (!ok && this._pinQueue === queued) this._pinQueue = Promise.resolve(true);
      return ok;
    });
    this._pinQueue = queued;
    return queued;
  }

  _companionComposerHtml() {
    // Mirrors the conversation composer: no clear control there, none here.
    return `<div class="composer">` +
      `<div class="note" data-comp-note role="status" hidden></div>` +
      this._errRowHtml('companion') +
      this._composerRowHtml('companion', 'Ask Ara …', false) + `</div>`;
  }

  // ── Wiring ─────────────────────────────────────────────────────────────────
  _wire() {
    const root = this.shadowRoot;
    // Back: a real link (its href is the fallback destination, and it still
    // opens the chats list in a new tab on a modified click) whose plain press
    // honours where the user actually came from — see _goBack.
    const back = root.querySelector('[data-back]');
    if (back) {
      back.addEventListener('click', (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
        e.preventDefault();
        this._goBack();
      });
    }
    // The model picker is re-rendered in place (see _syncPicker), so the
    // listener sits on the bar that survives, not on the select.
    const compBar = root.querySelector('[data-comp-bar]');
    if (compBar) {
      compBar.addEventListener('change', (e) => {
        const sel = e.target;
        if (sel && sel.matches && sel.matches('[data-model]')) this._onModelChange(sel.value);
      });
    }
    // Pane tabs (phone): scroll the snap strip; the scroll handler below keeps
    // the indicator honest whichever way the pane was reached (tab or swipe).
    root.querySelectorAll('[data-pane-tab]').forEach((el) =>
      el.addEventListener('click', () => this._setPane(el.getAttribute('data-pane-tab'), 'smooth')));
    const panes = root.querySelector('[data-panes]');
    if (panes) {
      let scrollT = null;
      panes.addEventListener('scroll', () => {
        if (this._wide.matches) return;
        if (scrollT) clearTimeout(scrollT);
        scrollT = setTimeout(() => {
          const idx = Math.round(panes.scrollLeft / Math.max(1, panes.clientWidth));
          const pane = idx >= 1 ? 'companion' : 'chat';
          if (pane !== this._pane) { this._pane = pane; this._markTabs(); }
        }, 80);
      }, { passive: true });
    }

    // Composers: the chat sends to the channel, the companion to Ara.
    this._wireChatComposer();
    this._wireComposer('companion');

    // Image lightbox: delegated on the thread container, so bubbles appended
    // by polls and sends are covered without per-message wiring.
    const chatThread = root.querySelector('[data-chat-thread]');
    if (chatThread) {
      chatThread.addEventListener('click', (e) => {
        const btn = e.target && e.target.closest && e.target.closest('[data-lightbox]');
        if (btn) {
          this._openLightbox(btn.getAttribute('data-lightbox'),
            btn.getAttribute('data-alt') || 'Image');
        }
      });
    }

    // Splitter (wide layout): drag / double-click reset / arrow keys, position
    // persisted per device — the layout.js interaction set, page-local.
    const splitter = root.querySelector('[data-splitter]');
    if (splitter) {
      splitter.addEventListener('pointerdown', (e) => {
        if (!this._wide.matches) return;
        e.preventDefault();
        splitter.setPointerCapture(e.pointerId);
        splitter.classList.add('dragging');
        const startX = e.clientX;
        const comp = root.querySelector('.pane-companion');
        const startW = comp ? comp.getBoundingClientRect().width : MIN_COMP_PX;
        const move = (ev) => {
          // The companion sits right of the splitter: growing it means
          // dragging the boundary left.
          this._sizes.comp = Math.round(this._clampComp(startW + (startX - ev.clientX)));
          this._applySizes();
        };
        const up = () => {
          splitter.classList.remove('dragging');
          splitter.removeEventListener('pointermove', move);
          splitter.removeEventListener('pointerup', up);
          splitter.removeEventListener('pointercancel', up);
          this._saveSizes();
        };
        splitter.addEventListener('pointermove', move);
        splitter.addEventListener('pointerup', up);
        splitter.addEventListener('pointercancel', up);
      });
      splitter.addEventListener('dblclick', () => {
        if (!this._wide.matches) return;
        delete this._sizes.comp;
        this._applySizes();
        this._saveSizes();
      });
      splitter.addEventListener('keydown', (e) => {
        if (!this._wide.matches) return;
        let delta = 0;
        if (e.key === 'ArrowLeft') delta = KEY_STEP_PX;
        else if (e.key === 'ArrowRight') delta = -KEY_STEP_PX;
        else if (e.key === 'Enter') {
          delete this._sizes.comp;
          this._applySizes();
          this._saveSizes();
          return;
        } else return;
        e.preventDefault();
        const comp = root.querySelector('.pane-companion');
        const cur = comp ? comp.getBoundingClientRect().width : MIN_COMP_PX;
        this._sizes.comp = Math.round(this._clampComp(cur + delta));
        this._applySizes();
        this._saveSizes();
      });
    }
  }

  // The chat composer plus what only it has: the quick-pattern chips and the
  // staged-image previews — everything _refreshChatComposer must re-wire
  // after replacing the composer block in place.
  _wireChatComposer() {
    const root = this.shadowRoot;
    // Quick patterns: pre-fill the companion composer with the canned prompt
    // and bring that pane forward — a chip is a companion turn, nothing more.
    root.querySelectorAll('[data-quick]').forEach((el) =>
      el.addEventListener('click', () => this._quickPattern(el.getAttribute('data-quick'))));
    // Preview removes live outside the input row, so they stay tappable even
    // while a recording or dictation job holds the row.
    root.querySelectorAll('[data-rmimg]').forEach((el) =>
      el.addEventListener('click', () => this._removeImage(Number(el.getAttribute('data-rmimg')))));
    this._wireComposer('chat');
  }

  // Rebuild only the chat composer block (chips, previews, error line, input
  // row) in place — never the thread, whose scroll position and playing media
  // a full render would reset.
  _refreshChatComposer() {
    const el = this.shadowRoot.querySelector('.pane-chat .composer');
    if (!el) { this.render(); return; }
    const tpl = document.createElement('template');
    tpl.innerHTML = this._chatComposerHtml();
    el.replaceWith(tpl.content.firstElementChild);
    this._wireChatComposer();
  }

  // Wire one composer: input tracking + autosize, the inline clear, the mic,
  // the image attach inputs, Cmd/Ctrl+Enter, submit — or, while this target
  // records, the recording row's three controls (voice.js renders them; the
  // host decides what they mean). A status row (dictation in flight) has
  // nothing to wire.
  _wireComposer(target) {
    const root = this.shadowRoot;
    const isChat = target === 'chat';
    if (this._recState === 'recording' && this._recTarget === target) {
      const pane = root.querySelector(isChat ? '.pane-chat' : '.pane-companion');
      if (!pane) return;
      const on = (sel, fn) => {
        const el = pane.querySelector(sel);
        if (el) el.addEventListener('click', fn);
      };
      on('[data-rec-abort]', () => this._abortRecording());
      on('[data-rec-check]', () => this._finishRecording('review'));
      on('[data-rec-send]', () => this._finishRecording('send'));
      return;
    }
    const form = root.querySelector(`[data-composer="${target}"]`);
    if (!form) return;
    if (isChat) {
      // The picked files are read before the input resets — resetting first
      // would hand _addImages an empty list.
      form.querySelectorAll('[data-attach]').forEach((inp) =>
        inp.addEventListener('change', () => {
          const files = Array.from(inp.files || []);
          inp.value = '';
          this._addImages(files);
        }));
    }
    const input = form.querySelector('textarea');
    const field = form.querySelector('[data-field]');
    const grow = () => {
      input.style.height = 'auto';
      input.style.height =
        `${Math.min(input.scrollHeight, Math.round(window.innerHeight * TEXTAREA_MAX_HEIGHT_RATIO))}px`;
    };
    input.addEventListener('input', () => {
      if (isChat) {
        this._draft = input.value;
        // The shared draft follows the keystrokes, debounced — an agent (and
        // another device) reads it from the chat state.
        this._scheduleDraftSave();
      } else {
        this._compDraft = input.value;
      }
      field.classList.toggle('has-text', !!input.value);
      // Anything typed (or dictated in) means the user has moved on; the offer
      // does not come back if they then delete it again.
      if (isChat && this._undo) this._setUndo(null);
      grow();
    });
    if (isChat) {
      input.addEventListener('blur', () => this._saveDraft());
    }
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        form.requestSubmit();
      }
    });
    grow();
    const clearBtn = form.querySelector('[data-clear]');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        // One tap to an empty box — the deliberate divergence from the real
        // clients. It also dismisses the staged-draft marker (the draft is
        // rejected, not sent) and clears the server-side draft with it. All of
        // which is why the tap is offered back: see _setUndo.
        const prev = input.value;
        const wasAra = this._draftByAra;
        input.value = '';
        this._draft = '';
        this._setDraftByAra(false);
        field.classList.remove('has-text');
        grow();
        input.focus();
        this._saveDraft();
        // Offered only for something the server actually held: it stores a
        // draft stripped, so whitespace alone was never a draft and there
        // would be nothing to put back — an undo that could only ever fail.
        this._setUndo(prev.trim() ? { text: prev, byAra: wasAra } : null);
      });
    }
    const undoBtn = form.querySelector('[data-undo]');
    if (undoBtn) undoBtn.addEventListener('click', () => this._undoClear());
    const mic = form.querySelector('[data-mic]');
    if (mic) {
      mic.addEventListener('click', () => {
        // The recording row replaces this one and the transcript lands in the
        // field, so the offer is spent either way.
        if (isChat) this._setUndo(null);
        this._startRecording(target);
      });
    }
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const text = input.value;
      // A chat message needs text or at least one staged image; a companion
      // turn is text-only.
      if (!text.trim() && !(isChat && this._outImages.length)) return;
      if (isChat) {
        this._draft = '';
        this._setDraftByAra(false);
        // Sending is moving on: an offer left over from an earlier clear must
        // not put that text back into a composer the user has just emptied on
        // purpose.
        this._setUndo(null);
      } else {
        this._compDraft = '';
      }
      input.value = '';
      field.classList.remove('has-text');
      grow();
      // After the field reset: _sendChat consumes the staged images and
      // replaces the composer block to drop their previews.
      if (isChat) this._sendChat(text);
      else this._sendCompanion(text);
    });
  }

  // ── Voice input: record → live waveform → transcribe (review or send) ──────
  // The same flow as the conversation composer and the project command bar:
  // the mic swaps this composer's input row for voice.js's recording row —
  // abort ✕ | live waveform | review ✓ | send ➤ — then a status line while
  // the dictation is transcribed (and, on the send path, sent).
  async _startRecording(target) {
    if (this._recState !== 'idle' || this._voiceJobs[target]) return;
    if (!canRecord()) {
      this._voiceErrors[target] = 'Voice recording is not supported on this device.';
      this.render();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this._recStream = stream;
      this._recChunks = [];
      this._recIntent = null;
      this._recAborted = false;
      this._recTarget = target;
      delete this._voiceErrors[target];
      const mr = new MediaRecorder(stream);
      this._mediaRecorder = mr;
      mr.addEventListener('dataavailable', (e) => {
        if (e.data && e.data.size) this._recChunks.push(e.data);
      });
      mr.addEventListener('stop', () => this._onRecordingStopped());
      mr.start();
      this._recState = 'recording';
      this.render();
      this._wave.start(stream);
    } catch (_err) {
      this._recState = 'idle';
      this._recTarget = null;
      this._voiceErrors[target] = 'Microphone access was denied.';
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
  // work continues in _onRecordingStopped once the recorder flushes its
  // chunks. A decision already taken (an earlier tap, or abort) wins.
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

  async _onRecordingStopped() {
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
    const target = this._recTarget || 'chat';
    this._recTarget = null;
    this._recState = 'idle';
    if (aborted || !chunks.length) {
      this.render();
      return;
    }
    this._voiceJobs[target] = { sending: intent === 'send', phase: 'transcribing' };
    this.render();
    let toSend = '';
    try {
      // Same endpoint and cleanup pass as the conversation composer. No
      // thread context is sent — chat ids are not conversation ids; the live
      // chat API can add its own context parameter later.
      const res = await fetch('/conversations/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': type || 'application/octet-stream' },
        body: new Blob(chunks, { type }),
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const text = ((data && data.text) || '').trim();
      if (text) {
        // Append to anything already typed — dictating into a staged agent
        // draft edits it exactly like typing (the marker stays until the
        // draft is cleared or sent).
        const cur = target === 'chat' ? this._draft : this._compDraft;
        const next = cur ? `${cur.replace(/\s*$/, '')} ${text}` : text;
        if (target === 'chat') this._draft = next; else this._compDraft = next;
        if (intent === 'send') toSend = next;
      } else {
        this._voiceErrors[target] = 'No speech was detected in the recording.';
      }
    } catch (_err) {
      this._voiceErrors[target] = "Couldn't transcribe the recording. Please try again.";
    }
    delete this._voiceJobs[target];
    if (toSend) {
      if (target === 'chat') { this._draft = ''; this._draftByAra = false; }
      else this._compDraft = '';
    }
    this.render();
    if (toSend) {
      // After the render, so the appended bubble (and any note) lands in the
      // fresh DOM instead of being wiped by it. A dictated turn takes the same
      // path as a typed one — to the channel, or to Ara.
      if (target === 'chat') this._sendChat(toSend);
      else this._sendCompanion(toSend);
    } else if (intent === 'review') {
      // Dictating into the field edits the shared draft like typing does.
      if (target === 'chat') this._scheduleDraftSave();
      // The deliberate review flow returns to the field for editing.
      const input = this.shadowRoot.querySelector(`[data-composer="${target}"] textarea`);
      if (input) { try { input.focus(); } catch (_e) { /* ignore */ } }
    }
  }

  // ── Behaviour ──────────────────────────────────────────────────────────────
  _setPane(pane, behavior) {
    this._pane = pane === 'companion' ? 'companion' : 'chat';
    this._markTabs();
    const panes = this.shadowRoot.querySelector('[data-panes]');
    if (!panes || this._wide.matches) return; // wide: both panes are visible
    const left = this._pane === 'companion' ? panes.clientWidth : 0;
    try { panes.scrollTo({ left, behavior: behavior || 'smooth' }); }
    catch (_e) { panes.scrollLeft = left; }
  }

  _markTabs() {
    this.shadowRoot.querySelectorAll('[data-pane-tab]').forEach((el) => {
      const on = el.getAttribute('data-pane-tab') === this._pane;
      el.classList.toggle('on', on);
      el.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  // ── Undoing a clear ────────────────────────────────────────────────────────
  // The ✕ empties the box in one tap, which is what makes it useful and also
  // what makes a mis-tap expensive: the text is gone from the field AND from
  // the server-side draft, and a draft Ara staged is not something the user
  // can retype. So a clear is offered back for a short while — as an undo in
  // the ✕'s own slot, costing no width, and gone again the moment it stops
  // being what the user wants.
  //
  // It ends on whichever comes first: the timeout, or anything that means the
  // user has moved on — typing, dictating, sending, or Ara staging a new
  // draft over it. Only a non-empty clear offers one; clearing an already
  // empty box has nothing to give back.
  _setUndo(entry) {
    if (this._undoTimer) clearTimeout(this._undoTimer);
    this._undoTimer = null;
    this._undo = entry || null;
    if (this._undo) {
      this._undoTimer = setTimeout(() => this._setUndo(null), UNDO_CLEAR_MS);
    }
    const field = this.shadowRoot.querySelector('[data-composer="chat"] [data-field]');
    if (field) field.classList.toggle('has-undo', !!this._undo);
  }

  // Put the cleared draft back — the SERVER's copy of it, not a rewrite of the
  // text from here. Two things follow from that, and both are the reason the
  // endpoint exists.
  //
  // The draft comes back with the author it had, so one Ara staged is still
  // marked as hers. Rewriting it from the client could only save it as the
  // user's own (the draft endpoint stamps author "user", as it must), and that
  // marker is what tells the user whose words they are about to send in their
  // name — precisely the thing worth not losing when the ✕ was a mis-tap.
  //
  // And it cannot lose a race with the clear's own save. The clear's write may
  // still be in flight; awaiting it first means the restore is applied to
  // settled state, and the restore is one guarded server-side step rather than
  // a second write that the in-flight one could overtake or the equality guard
  // swallow.
  //
  // The composer is refreshed rather than written directly because the "Draft
  // by Ara" marker is part of that block.
  async _undoClear() {
    const entry = this._undo;
    if (!entry) return;
    this._setUndo(null);
    // Held for the round trip. Any draft save landing in between would write
    // these words back as the USER's — the blur that replacing a focused
    // textarea fires, or the user simply tapping away — and that would both
    // lose the author and consume the server's stash before the restore asks
    // for it.
    this._undoing = true;
    // Optimistic, but written into the existing field rather than through a
    // composer rebuild: rebuilding blurs the focused textarea, and that blur
    // is itself one of the saves being guarded against. The "Draft by Ara"
    // marker arrives with the refresh below, one round trip later.
    this._draft = entry.text;
    const input = this.shadowRoot.querySelector('[data-composer="chat"] textarea');
    if (input) {
      input.value = entry.text;
      const field = input.closest('[data-field]');
      if (field) field.classList.add('has-text');
    }
    this._focusComposerEnd();
    if (this._draftInflight) {
      try { await this._draftInflight; } catch (_e) { /* its failure is its own */ }
    }
    try {
      const res = await fetch(
        `/chats/${encodeURIComponent(this._id)}/draft/undo`, { method: 'POST' });
      const data = await res.json();
      // Settle on what the server actually holds, whether it restored or
      // refused — a refusal means something else has since claimed the draft,
      // and showing our optimistic copy over it would be the stale view.
      const draft = (data && data.draft) || null;
      this._draftVersion = (data && data.version) || this._draftVersion;
      this._draft = (draft && draft.text) || '';
      this._draftSaved = this._draft;
      this._draftByAra = !!(draft && draft.author === 'agent');
      this._refreshChatComposer();
      if (this._draft) this._focusComposerEnd();
      if (!res.ok) this._showNote('That draft could not be restored.');
    } catch (_err) {
      // Offline: the words are on screen and the next edit saves them as the
      // user's own, which is the honest outcome when the restore never landed.
      this._draftSaved = '';
      this._undoing = false;
      this._scheduleDraftSave();
      return;
    } finally {
      this._undoing = false;
    }
  }

  _focusComposerEnd() {
    const input = this.shadowRoot.querySelector('[data-composer="chat"] textarea');
    if (!input) return;
    try {
      input.focus();
      // Caret at the end, where the user left off, not at the start.
      input.setSelectionRange(input.value.length, input.value.length);
    } catch (_e) { /* a browser that dislikes selection on a hidden field */ }
  }

  _setDraftByAra(on) {
    this._draftByAra = on;
    const tag = this.shadowRoot.querySelector('[data-draft-tag]');
    if (tag && !on) tag.remove();
  }

  _scrollThread(sel) {
    const el = this.shadowRoot.querySelector(sel);
    if (el) el.scrollTop = el.scrollHeight;
  }

  // ── Staged outgoing images ─────────────────────────────────────────────────
  async _addImages(files) {
    this._imgError = '';
    for (const file of files) {
      if (!String(file.type || '').startsWith('image/')) {
        this._imgError = `"${file.name}" is not an image.`;
        continue;
      }
      if (this._outImages.length >= MAX_IMAGES_PER_SEND) {
        this._imgError = `Up to ${MAX_IMAGES_PER_SEND} images per message.`;
        break;
      }
      try {
        const im = await this._prepareImage(file);
        if (im.blob.size > MAX_IMAGE_BYTES) {
          this._imgError = `"${file.name}" is too large.`;
          continue;
        }
        im.url = URL.createObjectURL(im.blob);
        im.name = file.name;
        this._outImages.push(im);
      } catch (_err) {
        this._imgError = `Couldn't read "${file.name}".`;
      }
    }
    this._refreshChatComposer();
    // Back to the field to type the caption (the conversations composer's
    // behaviour after attaching).
    const ta = this.shadowRoot.querySelector('[data-composer="chat"] textarea');
    if (ta) { try { ta.focus(); } catch (_e) { /* ignore */ } }
  }

  _removeImage(index) {
    const [im] = this._outImages.splice(index, 1);
    if (im) { try { URL.revokeObjectURL(im.url); } catch (_e) { /* ignore */ } }
    this._imgError = '';
    this._refreshChatComposer();
  }

  // Downscale/recompress one picked image the way the native clients do:
  // longest edge to IMAGE_MAX_EDGE, JPEG at IMAGE_JPEG_QUALITY — except
  // animated GIFs, which a canvas pass would freeze to one frame, so they
  // pass through unchanged (still under the size cap). Returns {blob,
  // content_type, width, height} — the real intrinsic size of what will be
  // sent, so the optimistic bubble reserves the same box the server's
  // ingest-sniffed dimensions will confirm.
  async _prepareImage(file) {
    if (file.type === 'image/gif' && await this._gifAnimated(file)) {
      const bmp = await this._decodeImage(file);
      return { blob: file, content_type: 'image/gif', width: bmp.width, height: bmp.height };
    }
    const bmp = await this._decodeImage(file);
    const scale = Math.min(1, IMAGE_MAX_EDGE / Math.max(bmp.width, bmp.height));
    const w = Math.max(1, Math.round(bmp.width * scale));
    const h = Math.max(1, Math.round(bmp.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    canvas.getContext('2d').drawImage(bmp, 0, 0, w, h);
    if (typeof bmp.close === 'function') { try { bmp.close(); } catch (_e) { /* ignore */ } }
    const blob = await new Promise((res, rej) => canvas.toBlob(
      (b) => (b ? res(b) : rej(new Error('encode'))), 'image/jpeg', IMAGE_JPEG_QUALITY));
    return { blob, content_type: 'image/jpeg', width: w, height: h };
  }

  // createImageBitmap honours EXIF orientation; the Image fallback covers
  // engines without it (a detached image's width/height are its intrinsic
  // size, so both paths read uniformly).
  async _decodeImage(file) {
    if (typeof createImageBitmap === 'function') {
      try { return await createImageBitmap(file, { imageOrientation: 'from-image' }); }
      catch (_e) { /* fall through — option unsupported or decode failed */ }
    }
    return new Promise((res, rej) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); res(img); };
      img.onerror = () => { URL.revokeObjectURL(url); rej(new Error('decode')); };
      img.src = url;
    });
  }

  // Animated = more than one Graphic Control Extension block (0x21 0xF9) in
  // the stream — cheap, and exact enough for real-world GIFs.
  async _gifAnimated(file) {
    const buf = new Uint8Array(await file.arrayBuffer());
    let count = 0;
    for (let i = 0; i + 1 < buf.length; i += 1) {
      if (buf[i] === 0x21 && buf[i + 1] === 0xf9) {
        count += 1;
        if (count > 1) return true;
      }
    }
    return false;
  }

  // {content_type, data}: the payload item the send endpoint accepts. Base64
  // via FileReader, so a large blob never hits a string-building loop.
  _imagePayload(im) {
    return new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res({
        content_type: im.content_type,
        data: String(r.result).split(',')[1] || '',
      });
      r.onerror = () => rej(new Error('read'));
      r.readAsDataURL(im.blob);
    });
  }

  // Live send: an optimistic bubble goes up at once — staged images showing
  // their local previews in the same reserved boxes the server's dimensions
  // will confirm — then is reconciled with the Message the server returns
  // (POST /chats/<id>/send — the gateway records author `user`, and no policy
  // category queues it: the user's send press IS the approval `verify` exists
  // for). On failure the bubble comes back down and the words AND the staged
  // images return to the composer for retry.
  async _sendChat(text) {
    if (this._draftTimer) clearTimeout(this._draftTimer);
    this._draftTimer = null;
    const images = this._outImages;
    this._outImages = [];
    this._imgError = '';
    if (images.length) {
      const ta = this.shadowRoot.querySelector('[data-composer="chat"] textarea');
      const hadFocus = !!ta && this.shadowRoot.activeElement === ta;
      this._refreshChatComposer();
      if (hadFocus) {
        const next = this.shadowRoot.querySelector('[data-composer="chat"] textarea');
        if (next) { try { next.focus(); } catch (_e) { /* ignore */ } }
      }
    }
    // Order matters server-side: let a save fired by the pre-tap blur settle
    // before the send clears the draft.
    if (this._draftInflight) { try { await this._draftInflight; } catch (_e) { /* ignore */ } }
    this._localSeq += 1;
    const local = {
      id: `local-${this._localSeq}`,
      chat: this._chat.id,
      direction: 'out',
      author: 'user',
      text,
      ts: new Date().toISOString(),
    };
    if (images.length) {
      local.attachments = images.map((im, i) => ({
        id: `local-att-${this._localSeq}-${i}`,
        url: im.url,
        type: im.content_type,
        width: im.width,
        height: im.height,
        name: im.name,
      }));
    }
    this._messages.push(local);
    this._appendChatMessage(local);
    const findLocal = () => this.shadowRoot.querySelector(
      `[data-chat-thread] [data-mid="${local.id}"]`);
    const node = findLocal();
    if (node) node.classList.add('sending');
    try {
      const body = {};
      if (!images.length || text.trim()) body.text = text;
      if (images.length) {
        body.images = await Promise.all(images.map((im) => this._imagePayload(im)));
      }
      const res = await fetch(`/chats/${encodeURIComponent(this._id)}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(String(res.status));
      const msg = await res.json();
      // Reconcile: the server's message replaces the optimistic bubble
      // wholesale — same renderer, so nothing moves except the media sources,
      // now the proxied URLs (the reserved boxes match by construction), and
      // the poll's de-dup knows the message.
      const idx = this._messages.indexOf(local);
      if (idx >= 0) this._messages[idx] = msg;
      // An unconfirmed send has no identity to remember: its id is synthesised
      // from a timestamp of ours, so registering it as seen would not stop the
      // real record — which carries the channel's own id and instant — from
      // being appended as a second copy of the user's own message. It is
      // reconciled by its words instead, in _reconcileUnconfirmed.
      if (msg.unconfirmed) {
        this._unconfirmed.push({ id: msg.id, text: msg.text || '',
                                 since: msg.since || '' });
      } else {
        this._seen.add(msg.id);
      }
      const cur = findLocal();
      if (cur) {
        const tpl = document.createElement('template');
        tpl.innerHTML = this._chatMsgHtml(msg);
        cur.replaceWith(tpl.content.firstElementChild);
      }
      images.forEach((im) => { try { URL.revokeObjectURL(im.url); } catch (_e) { /* ignore */ } });
      // The server cleared the shared draft and advanced the watermark with
      // this send; mirror both (the next draft save re-anchors its version
      // through the guard's one-shot retry).
      this._draftSaved = '';
      this._postRead(msg.ts);
    } catch (_err) {
      // Not sent: take the bubble down, give words and images back to the
      // composer. The preview object URLs were never revoked, so the staged
      // images are intact for the retry.
      const idx = this._messages.indexOf(local);
      if (idx >= 0) this._messages.splice(idx, 1);
      const cur = findLocal();
      if (cur) {
        const sep = cur.previousElementSibling;
        cur.remove();
        // A day separator introduced just for this bubble goes with it.
        if (sep && sep.classList.contains('day-sep') && sep.nextElementSibling === null) sep.remove();
      }
      if (images.length) {
        this._outImages = images.concat(this._outImages);
        this._draft = text;
        this._refreshChatComposer();
      } else {
        this._restoreComposer(text);
      }
      this._showNote("Couldn't send &mdash; check the connection and try again.");
    }
  }

  _restoreComposer(text) {
    this._draft = text;
    const form = this.shadowRoot.querySelector('[data-composer="chat"]');
    const input = form && form.querySelector('textarea');
    if (!input) return;
    input.value = text;
    input.dispatchEvent(new Event('input'));
  }

  // One mirrored message onto the end of the thread. The companion pane has no
  // counterpart: its turns are few and carry no media, so it re-renders whole
  // (_renderCompanion) rather than tracking what is already on screen.
  _appendChatMessage(m, stick = true) {
    const thread = this.shadowRoot.querySelector('[data-chat-thread]');
    if (!thread) return;
    const prev = [...this._messages].reverse().find((x) => x !== m);
    let html = '';
    if (!prev || dayKey(prev.ts) !== dayKey(m.ts)) {
      html += `<div class="day-sep"><span>${esc(fmtDay(m.ts))}</span></div>`;
    }
    html += this._chatMsgHtml(m);
    // A message arriving while the user reads old history must not yank the
    // view to the bottom; only stick when they were already there.
    const keep = thread.scrollTop;
    thread.insertAdjacentHTML('beforeend', html);
    thread.scrollTop = stick ? thread.scrollHeight : keep;
  }

  // A transient line above one composer. Each pane keeps its own timer, so a
  // note in the chat cannot cut one in the companion short.
  _showNote(html, target = 'chat') {
    const isChat = target === 'chat';
    const note = this.shadowRoot.querySelector(isChat ? '[data-note]' : '[data-comp-note]');
    if (!note) return;
    const thread = isChat ? '[data-chat-thread]' : '[data-comp-thread]';
    note.innerHTML = html;
    note.hidden = false;
    // The note row grows the composer at the thread's expense; keep the
    // newest bubble in view through both height changes.
    this._scrollThread(thread);
    if (this._noteTimers[target]) clearTimeout(this._noteTimers[target]);
    this._noteTimers[target] = setTimeout(() => {
      note.hidden = true;
      this._scrollThread(thread);
    }, NOTE_MS);
  }

  // A chip is a companion turn with a canned prompt over the current draft:
  // it brings the pane forward and runs, exactly as if the user had typed it.
  _quickPattern(id) {
    const p = QUICK_PATTERNS.find((x) => x.id === id);
    if (!p) return;
    // Post first, switch after: the turn's optimistic render lands before the
    // pane starts moving, so the phone's snap strip is not re-snapped
    // mid-scroll (which would drop the user back on the chat pane).
    this._sendCompanion(p.prompt(this._draft.trim()));
    this._setPane('companion', 'smooth');
  }

  // ── The companion thread ───────────────────────────────────────────────────
  // Everything past the thread's id is the plain conversation API: the reply
  // POST returns the thread with the user's turn appended and `pending` set,
  // and the poll below carries Ara's answer in when it lands.

  // The thread is created on demand, once, however many callers race for it
  // (a chip tap while a typed turn is still creating). A chat merely opened
  // never reaches here, so it never gets a thread.
  _ensureCompanion() {
    if (this._companionId) return Promise.resolve(this._companionId);
    if (!this._compCreate) {
      this._compCreate = (async () => {
        const res = await fetch(`/chats/${encodeURIComponent(this._id)}/companion`,
          { method: 'POST' });
        if (!res.ok) throw new Error(String(res.status));
        const data = await res.json();
        const id = (data && data.id) || '';
        if (!id) throw new Error('no companion id');
        this._companionId = id;
        // A fresh thread has nothing to read: it runs the default until
        // pinned, and its picker is live at once.
        this._compLoaded = true;
        // A choice made before the thread existed is pinned now, ahead of
        // the first turn that is about to be posted into it. If the gateway
        // refuses the pin, that turn does not go out on a model the user did
        // not choose: the picker drops back to the default the thread really
        // has, and the caller reports the failure. A pick the user replaced
        // while this one was in flight is not what decides: the queue is
        // settled, and only a refusal of the latest choice aborts the turn.
        const held = this._compModel;
        if (held) {
          this._pinAndReport(id, held);
          if (!(await this._settledPins())) throw new Error('model');
        }
        return id;
      })().finally(() => { this._compCreate = null; });
    }
    return this._compCreate;
  }

  async _loadCompanion() {
    if (!this._companionId || document.hidden) return;
    try {
      // Read only once no pin is in flight: a GET that overlaps a pin can
      // carry the previous model under the newest generation, and would be
      // adopted as current.
      await this._settledPins();
      const gen = this._pinGen;
      const res = await fetch(`/conversations/${encodeURIComponent(this._companionId)}`,
        { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      this._adoptCompanion(await res.json(), gen);
    } catch (_err) {
      // Offline or gateway blip: keep what is on screen; the next poll
      // reconciles, exactly as the mirror does.
    }
  }

  // The conversation the API returned is the pane's truth — its messages and
  // whether Ara is still working. `gen` is the pin generation when the
  // request began: its model fields count only if no pin has started since,
  // or a slow poll from before a switch would put the old model back.
  _adoptCompanion(conv, gen = this._pinGen) {
    if (!conv) return;
    if (conv.id) this._companionId = conv.id;
    this._companion = Array.isArray(conv.messages) ? conv.messages : [];
    this._compPending = !!conv.pending;
    this._compStatus = conv.pending_status || '';
    this._compLoaded = true;
    if (gen === this._pinGen) {
      // The thread's stored choice is the truth once it exists. The document
      // carries the field only once a choice was ever made; absent means the
      // gateway default, exactly like '' — unless the thread is escalated,
      // which the picker shows as its own state.
      this._compModel = typeof conv.model === 'string' ? conv.model : '';
      this._compModelConfirmed = this._compModel;
      this._compEscalated = !!conv.escalated;
    }
    this._renderCompanion();
    this._syncPicker();
    // Reading the pane is reading the thread: the dashboard must not badge a
    // companion turn the user has already seen here.
    if (conv.unread) this._markCompanionRead();
  }

  async _markCompanionRead() {
    try {
      await fetch(`/conversations/${encodeURIComponent(this._companionId)}/read`,
        { method: 'POST' });
    } catch (_err) { /* the badge is cosmetic; a later load retries */ }
  }

  // Self-rescheduling rather than an interval: the cadence changes with the
  // pending flag, and no two polls can overlap.
  _scheduleCompanionPoll() {
    if (this._compTimer) clearTimeout(this._compTimer);
    this._compTimer = null;
    if (!this._companionId) return;
    this._compTimer = setTimeout(async () => {
      this._compTimer = null;
      await this._loadCompanion();
      this._scheduleCompanionPoll();
    }, this._compPending ? COMP_PENDING_POLL_MS : POLL_MS);
  }

  // Replace the companion thread in place — never a full page render, which
  // would rebuild the mirror (its scroll position and playing media) and the
  // chat composer along with it.
  _renderCompanion() {
    const el = this.shadowRoot.querySelector('[data-comp-thread]');
    if (!el) return;
    const panes = this.shadowRoot.querySelector('[data-panes]');
    const left = panes ? panes.scrollLeft : 0;
    const stick = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
    el.innerHTML = this._companionThreadHtml();
    if (stick) el.scrollTop = el.scrollHeight;
    // Changing a pane's contents re-evaluates the strip's scroll snapping; an
    // answer arriving must not slide the phone back onto the other pane.
    if (panes && panes.scrollLeft !== left) panes.scrollLeft = left;
  }

  // One turn: the user's words go up optimistically and Ara is shown working
  // from the moment they leave, so the pane never looks idle while a turn is
  // in flight. A failure takes the bubble back down and returns the words to
  // the composer — nothing typed is lost to a dropped connection.
  async _sendCompanion(text) {
    const local = { role: 'user', text, ts: new Date().toISOString() };
    this._companion = this._companion.concat([local]);
    this._compPending = true;
    this._compStatus = '';
    this._renderCompanion();
    try {
      const cid = await this._ensureCompanion();
      // A pin still in flight must be stored before this turn starts, or the
      // gateway may run it on the model the picker no longer shows — and a
      // pin the gateway refused means this turn does not go out at all
      // (_onModelChange has already put the picker back and said so).
      if (!(await this._settledPins())) throw new Error('model');
      const gen = this._pinGen;
      const res = await fetch(`/conversations/${encodeURIComponent(cid)}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      });
      if (!res.ok) throw new Error(String(res.status));
      this._adoptCompanion(await res.json(), gen);
    } catch (err) {
      const i = this._companion.indexOf(local);
      if (i >= 0) this._companion.splice(i, 1);
      this._compPending = false;
      this._renderCompanion();
      this._restoreCompanionComposer(text);
      this._showNote(err && err.message === 'model'
        ? "Couldn't set the model for this thread &mdash; it keeps the one it had, " +
          'and your message is back in the box.'
        : "Couldn't reach Ara &mdash; your message is back in the box.",
        'companion');
    }
    this._scheduleCompanionPoll();
  }

  _restoreCompanionComposer(text) {
    this._compDraft = text;
    const input = this.shadowRoot.querySelector('[data-composer="companion"] textarea');
    if (!input) return; // a dictation row holds the composer; the draft shows on its return
    input.value = text;
    input.dispatchEvent(new Event('input'));
  }
}

const CSS = `
  :host { display: flex; flex-direction: column; min-height: 0; }
  * { box-sizing: border-box; }
  .page { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .center { flex: 1; display: flex; flex-direction: column; align-items: center;
            justify-content: center; gap: 6px; text-align: center; padding: 24px 12px; }
  .muted { color: var(--muted, #8b93a3); }
  .backlink { color: var(--accent, #6ea8fe); text-decoration: none; }

  /* ── Header ──────────────────────────────────────────────────────────────── */
  .chat-head { flex: none; display: flex; align-items: center; gap: 10px; padding: 2px 0 10px;
               border-bottom: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .back { flex: none; width: 34px; height: 34px; border-radius: 50%;
          background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); text-decoration: none;
          font-size: 1.35rem; line-height: 1; display: inline-flex; align-items: center;
          justify-content: center; padding: 0 2px 2px 0; -webkit-tap-highlight-color: transparent; }
  .av { flex: none; position: relative; width: 40px; height: 40px; border-radius: 50%;
        display: inline-flex; align-items: center; justify-content: center;
        color: rgba(255, 255, 255, .92); font-size: .85rem; font-weight: 700; }
  .ch { position: absolute; right: -3px; bottom: -3px; width: 16px; height: 16px;
        border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
        font-size: .58rem; font-weight: 800; color: #fff;
        border: 2px solid var(--bg, #0b0d12); box-sizing: content-box; }
  .head-txt { flex: 1; min-width: 0; }
  .head-name { font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .head-sub { display: block; color: var(--muted, #8b93a3); font-size: .74rem;
              overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pane-tabs { flex: none; display: flex; background: var(--card-2, #1c2230);
               border-radius: 999px; padding: 3px; }
  .pane-tabs button { border: 0; background: transparent; color: var(--muted, #8b93a3);
                      border-radius: 999px; padding: 6px 14px; font: inherit; font-size: .8rem;
                      cursor: pointer; -webkit-tap-highlight-color: transparent; }
  .pane-tabs button.on { background: var(--accent, #6ea8fe); color: #0b0d12; font-weight: 600; }

  /* ── Panes: swipe strip on the phone, columns behind a splitter when wide ── */
  .panes { flex: 1; min-height: 0; display: flex;
           overflow-x: auto; scroll-snap-type: x mandatory; overscroll-behavior-x: contain;
           scrollbar-width: none; }
  .panes::-webkit-scrollbar { display: none; }
  .pane { flex: 0 0 100%; min-width: 0; scroll-snap-align: start; scroll-snap-stop: always;
          display: flex; flex-direction: column; min-height: 0; }
  .pane-splitter { display: none; }
  .comp-bar { flex: none; display: flex; align-items: baseline; gap: 8px; padding: 8px 2px 6px;
              border-bottom: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .comp-who { font-weight: 650; font-size: .85rem; }
  .comp-hint { color: var(--muted, #8b93a3); font-size: .72rem; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }
  .comp-actions { flex: none; margin-left: auto; display: inline-flex; align-items: center;
                  align-self: center; }
  /* The picker, as the conversations card styles it. */
  .model-pick { flex: none; display: inline-flex; align-items: center; gap: 3px;
                color: var(--muted, #8b93a3); }
  .model-pick .mp-ico { font-size: .9rem; line-height: 1; }
  .model-pick select { background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2);
                       border: 1px solid var(--line, rgba(231, 235, 242, .08));
                       border-radius: 999px; padding: 5px 8px; font-size: .74rem;
                       max-width: 9.5rem; cursor: pointer; -webkit-appearance: none;
                       appearance: none; }
  .model-pick select:hover { border-color: var(--accent, #6ea8fe); }
  @media ${WIDE_FRAME} {
    .panes { overflow-x: visible; scroll-snap-type: none; }
    .pane-chat { flex: 1 1 auto; }
    .pane-companion { flex: 0 0 auto; width: var(--comp-w, clamp(300px, 32vw, 440px));
                      min-width: 280px; max-width: 60%; }
    .pane-tabs { display: none; }
    /* Same look and hit area as the dashboard's splitters (styles.css). */
    .pane-splitter { display: block; flex: none; position: relative; z-index: 2; width: 14px;
                     cursor: col-resize; touch-action: none; user-select: none;
                     -webkit-user-select: none; }
    .pane-splitter::before { content: ""; position: absolute; top: 8px; bottom: 8px; left: 6px;
                             width: 2px; background: var(--line, rgba(231, 235, 242, .08)); }
    .pane-splitter:hover::before, .pane-splitter:focus-visible::before,
    .pane-splitter.dragging::before { background: var(--accent, #6ea8fe); }
    .pane-splitter:focus-visible { outline: none; }
  }

  /* ── The mirror ──────────────────────────────────────────────────────────── */
  .thread { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
            display: flex; flex-direction: column; gap: 10px; padding: 12px 2px; }
  .day-sep, .unread-sep { display: flex; align-items: center; gap: 10px;
                          color: var(--muted, #8b93a3); font-size: .72rem; margin: 4px 0; }
  .day-sep::before, .day-sep::after, .unread-sep::before, .unread-sep::after {
    content: ""; flex: 1; border-top: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .day-sep span { flex: none; }
  .unread-sep { color: var(--accent, #6ea8fe); }
  .unread-sep::before, .unread-sep::after { border-color: var(--accent, #6ea8fe); opacity: .5; }
  .msg { display: flex; flex-direction: column; gap: 2px; max-width: 86%; align-self: flex-start; }
  .msg.out { align-self: flex-end; align-items: flex-end; }
  .msg-head { display: flex; align-items: baseline; gap: 6px; padding: 0 4px; }
  .who { color: var(--muted, #8b93a3); font-size: .7rem; }
  .who.sender { font-weight: 650; }
  .bubble { background: var(--card-2, #1c2230); border-radius: 16px; border-bottom-left-radius: 6px;
            padding: 7px 12px 6px; line-height: 1.4; overflow-wrap: anywhere; }
  .msg.out .bubble { background: var(--accent, #6ea8fe); color: #0b0d12;
                     border-radius: 16px; border-bottom-right-radius: 6px; }
  /* Agent-sent: outbound but visually distinct from the user's own words. */
  .msg.by-agent .bubble { background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2);
                          border: 1px solid var(--accent, #6ea8fe); }
  .msg.by-agent .who { color: var(--accent, #6ea8fe); font-weight: 650; }
  .bubble a { color: var(--accent, #6ea8fe); text-decoration: underline; }
  .msg.out .bubble a { color: #0b0d12; }
  .msg.by-agent .bubble a { color: var(--accent, #6ea8fe); }
  .stamp { display: inline-block; float: right; margin: 8px 0 0 8px;
           font-size: .66rem; opacity: .6; }
  /* With intrinsic dimensions the true aspect box is reserved before the
     bytes arrive: the inline aspect-ratio + fixed-length width from
     _attachmentHtml size the element, height:auto follows the ratio, and
     max-width:100% clamps inside a narrower bubble. */
  .att-imgbtn { display: block; padding: 0; border: 0; background: transparent;
                cursor: zoom-in; border-radius: 10px;
                -webkit-tap-highlight-color: transparent; }
  .att-imgbtn:focus-visible { outline: 2px solid var(--accent, #6ea8fe); outline-offset: 1px; }
  .att-img { display: block; max-width: 100%; height: auto;
             border-radius: 10px; margin: 2px 0 6px; }
  /* No intrinsic dimensions in the record (older ledger entries): a fixed
     frame keeps the box stable through the lazy load (object-fit crops
     rather than reflows). The width is a fixed length on purpose — a
     percentage re-resolves against the shrink-to-fit bubble once the
     intrinsic ratio arrives, which would narrow the frame mid-load. */
  .att-img.no-dims { width: 220px; max-width: 100%; height: 160px; object-fit: cover;
                     background: rgba(0, 0, 0, .2); }
  /* A voice note's player sits above the transcript (the message text). Fixed
     length + clamp, not a percentage — see .att-img.no-dims. */
  .att-audio { display: block; width: 250px; max-width: 100%; height: 40px; margin: 2px 0 6px; }
  .att-video { display: block; max-width: 100%; height: auto;
               border-radius: 10px; margin: 2px 0 6px; background: rgba(0, 0, 0, .25); }
  /* Dimension-less video: the same fixed-frame reservation as images (fixed
     length, not a percentage — see .att-img.no-dims); the element letterboxes
     the frames inside the held box once metadata lands. */
  .att-video.no-dims { width: 280px; max-width: 100%; height: 180px; }
  /* A file row is a link like any other in a bubble (the bubble's own link
     styling applies); the size sits beside the name, quieter. */
  .att-file { display: block; font-size: .82rem; margin: 2px 0 4px; overflow-wrap: anywhere; }
  .att-size { opacity: .7; }
  /* Optimistic bubble awaiting the server's send confirmation. */
  .msg.sending .bubble { opacity: .7; }

  /* ── Lightbox ────────────────────────────────────────────────────────────── */
  .lightbox { position: fixed; inset: 0; z-index: 60; background: rgba(4, 6, 10, .93);
              display: flex; align-items: center; justify-content: center; outline: none; }
  .lightbox .lb-img { max-width: 100vw; max-height: 100vh; max-height: 100dvh;
                      object-fit: contain; }
  .lightbox .lb-close { position: fixed; top: calc(env(safe-area-inset-top, 0px) + 10px);
                        right: calc(env(safe-area-inset-right, 0px) + 10px);
                        width: 40px; height: 40px; border-radius: 50%; border: 0;
                        display: inline-flex; align-items: center; justify-content: center;
                        background: rgba(255, 255, 255, .14); color: #fff; font-size: 1rem;
                        cursor: pointer; -webkit-tap-highlight-color: transparent; }
  .lightbox .lb-close:hover { background: rgba(255, 255, 255, .28); }

  /* ── Composer ────────────────────────────────────────────────────────────── */
  .composer { flex: none; margin-top: 4px; padding: 10px 2px 2px;
              border-top: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .qchip { border: 1px solid var(--line, rgba(231, 235, 242, .12)); border-radius: 999px;
           background: var(--card-2, #1c2230); color: var(--muted, #8b93a3); cursor: pointer;
           padding: 4px 12px; font: inherit; font-size: .76rem;
           -webkit-tap-highlight-color: transparent; }
  .qchip:hover { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }
  .note { color: var(--muted, #8b93a3); font-size: .76rem; font-style: italic; margin-bottom: 8px; }
  .draft-tag { display: inline-flex; align-items: center; gap: 6px; margin-bottom: 8px;
               padding: 3px 10px; border-radius: 999px; border: 1px solid var(--accent, #6ea8fe);
               background: rgba(110, 168, 254, .12); color: var(--accent, #6ea8fe);
               font-size: .74rem; font-weight: 600; }
  .row { display: flex; gap: 6px; align-items: flex-end; }
  /* The attach control, inside the field and dressed exactly as the
     conversations composer's clip — same size, same corner, same hover. Two
     composers, one place to reach for a paperclip. */
  .clip { position: absolute; right: 3px; bottom: 3px; display: inline-flex;
          align-items: center; justify-content: center; height: 34px; width: 34px;
          border-radius: 50%; background: transparent; color: var(--muted, #8b93a3);
          cursor: pointer; font-size: 1rem; user-select: none;
          -webkit-tap-highlight-color: transparent; }
  .clip:hover { background: rgba(110, 168, 254, .2); }
  /* Staged images: removable thumbnails riding above the input row. */
  .img-previews { display: flex; flex-wrap: wrap; gap: 8px; margin: 2px 0 10px; }
  .imgp { position: relative; }
  .imgp img { display: block; width: 56px; height: 56px; object-fit: cover;
              border-radius: 10px; background: var(--card-2, #1c2230); }
  .imgp-x { position: absolute; top: -6px; right: -6px; width: 22px; height: 22px;
            display: inline-flex; align-items: center; justify-content: center;
            border: 0; border-radius: 50%; padding: 0; cursor: pointer;
            background: var(--high, #ff6b6b); color: #fff; font-size: .66rem;
            -webkit-tap-highlight-color: transparent; }
  .field { flex: 1; min-width: 0; position: relative; display: flex; }
  .row textarea { flex: 1; min-width: 0; min-height: 40px; max-height: 35vh;
                  background: var(--card-2, #1c2230); border: 0; border-radius: 20px;
                  padding: 9px 14px; color: var(--fg, #e7ebf2); font: inherit; line-height: 1.35;
                  resize: none; overflow-y: auto; }
  .row textarea::placeholder { color: var(--muted, #8b93a3); }
  .row textarea:focus-visible { outline: 1px solid rgba(110, 168, 254, .45); outline-offset: 0; }
  /* Only the send button is styled here — the mic and the recording/status
     rows take their look verbatim from the shared VOICE_CSS (voice.js),
     appended after this sheet, so all three composers in the app stay
     identical by construction. */
  .send { flex: none; display: inline-flex; align-items: center; justify-content: center;
          width: 40px; height: 40px; border-radius: 50%; border: 0; font-size: 1.05rem;
          cursor: pointer; padding: 0 0 0 2px; background: var(--accent, #6ea8fe);
          color: #0b0d12; -webkit-tap-highlight-color: transparent; }
  .send:active { filter: brightness(1.12); }
  /* The inline clear: docked in the field's top-right corner, shown only
     while there is text (see _composerRowHtml for the placement rationale);
     the .has-text padding keeps text from wrapping under it. */
  /* The clear ✕ sits at the field's bottom-right too, immediately left of the
     clip — both anchored to the same edge and the same baseline, so a one-line
     field cannot stack them on top of each other. It is only there while there
     is something to clear, and the textarea's right padding tracks whichever
     of the two is actually present. */
  .clear-inline { position: absolute; bottom: 5px; right: 5px; width: 30px; height: 30px;
                  display: inline-flex; align-items: center; justify-content: center;
                  border: 0; border-radius: 50%; padding: 0; cursor: pointer;
                  background: transparent; color: var(--muted, #8b93a3); font-size: .9rem;
                  -webkit-tap-highlight-color: transparent; }
  /* Beside the clip, not above it: both are anchored to the field's bottom
     edge, so stacking them would put one on top of the other in a one-line
     field. The ✕ takes the inner position — it acts on the text, so it sits
     nearer to it. */
  .field.has-clip .clear-inline { right: 39px; }
  .clear-inline:hover { color: var(--high, #ff6b6b); background: rgba(255, 107, 107, .14); }
  /* The undo occupies the ✕'s slot, in the accent colour — it offers something
     back rather than taking it away, and it should read as the one thing worth
     tapping in an unexpectedly empty box. */
  .undo-inline { position: absolute; bottom: 5px; right: 5px; width: 30px; height: 30px;
                 display: inline-flex; align-items: center; justify-content: center;
                 border: 0; border-radius: 50%; padding: 0; cursor: pointer;
                 background: transparent; color: var(--accent, #6ea8fe); font-size: 1rem;
                 -webkit-tap-highlight-color: transparent; }
  .field.has-clip .undo-inline { right: 39px; }
  .undo-inline:hover { background: rgba(110, 168, 254, .2); }
  /* One slot, two controls: the ✕ while there is text to clear, the undo while
     a clear is still recoverable and the box is empty. Text always wins — once
     the user is writing again there is nothing to undo. */
  .field:not(.has-text) .clear-inline { display: none; }
  .field:not(.has-undo) .undo-inline,
  .field.has-text .undo-inline { display: none; }
  /* Right padding tracks what is actually shown, so an empty field is not
     holding room for a control that is not there. */
  .field.has-clip textarea { padding-right: 42px; }
  .field.has-clear.has-text textarea { padding-right: 44px; }
  .field.has-clip.has-clear.has-text textarea,
  .field.has-clip.has-undo:not(.has-text) textarea { padding-right: 74px; }
  .attach-err { color: var(--high, #ff6b6b); font-size: .76rem; margin-bottom: 8px; }

  /* ── Companion pane (the conversation thread's visual language) ──────────── */
  .cmsg { display: flex; flex-direction: column; gap: 3px; max-width: 86%; align-self: flex-start; }
  .cmsg.me { align-self: flex-end; align-items: flex-end; }
  .cmsg-head { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
  .cmsg.me .cmsg-head { flex-direction: row-reverse; }
  /* model · ~$cost · time, one muted line — the conversations card's meta in a
     narrower pane, so it is allowed to wrap rather than push the head wider
     than the bubble. */
  .cmeta { color: var(--muted, #8b93a3); font-size: .7rem;
           display: inline-flex; align-items: baseline; gap: 5px; flex-wrap: wrap;
           min-width: 0; }
  .cmeta .m-sep { opacity: .5; }
  .cmeta .m-cost { font-variant-numeric: tabular-nums; }
  .cmeta .m-model { font-weight: 600; }
  .cbubble { background: var(--card-2, #1c2230); border-radius: 16px;
             border-bottom-left-radius: 6px; padding: 9px 13px; line-height: 1.4; }
  .cmsg.me .cbubble { background: var(--accent, #6ea8fe); color: #0b0d12;
                      border-bottom-left-radius: 16px; border-bottom-right-radius: 6px; }
  .cmsg.me .cbubble .md a { color: #0b0d12; }
  .cattach { display: inline-flex; align-items: center; gap: 6px; margin-top: 6px;
             color: var(--accent, #6ea8fe); font-size: .82rem; text-decoration: none; }
  .cattach:hover { text-decoration: underline; }
  .cmsg.me .cbubble .cattach { color: #0b0d12; }
  /* Ara's turn in flight: a quiet bubble holding her place in the thread. */
  .cpending { display: inline-flex; align-items: center; gap: 8px;
              color: var(--muted, #8b93a3); font-size: .84rem; font-style: italic; }
  .cdots { display: inline-flex; gap: 3px; }
  .cdots i { width: 5px; height: 5px; border-radius: 50%;
             background: var(--accent, #6ea8fe); opacity: .35;
             animation: cblink 1.2s infinite ease-in-out; }
  .cdots i:nth-child(2) { animation-delay: .18s; }
  .cdots i:nth-child(3) { animation-delay: .36s; }
  @keyframes cblink { 0%, 70%, 100% { opacity: .35; } 35% { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) {
    .cdots i { animation: none; opacity: .7; }
  }
  .comp-empty .e-ico { font-size: 2rem; opacity: .55; }
  .comp-empty p { margin: 0; max-width: 32ch; }
`;

customElements.define('retinue-chat-page', RetinueChatPage);
