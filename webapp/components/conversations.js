// Conversation tabs: standalone chat threads with Ara.
//
// Unlike the other cards (which render one static JSON document), this card is
// interactive and talks to the gateway's conversation API:
//   GET  /conversations                 list of active threads (tabs)
//   GET  /conversations?archived=1      list of archived threads
//   GET  /conversations/<id>            one thread with its messages
//   POST /conversations                 open a new thread ({message})
//   POST /conversations/<id>/messages   reply in a thread ({message})
//   POST /conversations/<id>/read       clear a thread's unread badge
//   POST /conversations/<id>/archive    archive a thread (hide from active list)
//   POST /conversations/<id>/unarchive  restore an archived thread
//
// A thread can also be opened by a retinue agent that needs a decision (via the
// gateway's token-gated /internal/conversations endpoint); such threads simply
// appear here with an unread badge and Ara engages once the user replies.
//
// The element runs in two modes. By default it is a compact dashboard card that
// shows the most recent active threads (capped at MAX_CARD_THREADS) plus a link
// to the dedicated all-conversations page, so the dashboard stays uncluttered.
// With the `full` attribute (used on conversations.html) it shows every thread
// with an Active/Archived filter and no cap.
//
// Ara answers asynchronously: a reply marks the thread `pending`, so this card
// polls until the answer arrives. Everything degrades gracefully offline (the
// list/threads just fail to refresh; the last rendered state stays on screen).

import {
  esc, fmtAge, isWideFrame, onFrameChange,
  viewPref, setViewPref, viewToggleHtml, VIEW_TOGGLE_CSS,
} from './base.js';
import { renderMarkdown, MD_CSS } from './markdown.js';
import { canRecord, recordingRowHtml, statusRowHtml, Waveform, VOICE_CSS } from './voice.js';
import { Reader, speechAvailable } from './speech.js';

const LIST_URL = '/conversations';
// Views are addressable by location hash, so opening a thread or the composer
// pushes a history entry and the platform back gesture returns to the list
// instead of leaving the PWA. The 32-hex id format must stay in sync with the
// gateway (_CONV_ID_RE); agent push URLs deep-link with the same hash.
const CONV_HASH_RE = /^#conversation-([0-9a-f]{32})$/;
// The composer hash may carry a project link (from a project page's "Discuss
// with Ara"): #new?project=<encoded uri>&title=<encoded title>.
const COMPOSER_HASH = '#new';
const COMPOSER_HASH_RE = /^#new(?:\?(.*))?$/;
// Most recent threads shown on the dashboard card before the user is sent to
// the dedicated all-conversations page for the rest. This cap exists to keep the
// PAGE short on the phone layout — in the wide layout the list scrolls inside
// its own column, so it is lifted there (see _shownThreads).
const MAX_CARD_THREADS = 5;
const POLL_MS = 4000;
const PENDING_WARN_SECONDS = 2 * 60;
const PENDING_STALE_SECONDS = 10 * 60;
const TEXTAREA_MAX_HEIGHT_RATIO = 0.35;
// Keep the client cap in step with the gateway's CONVERSATION_MAX_ATTACHMENT_BYTES
// (default 25 MiB) so oversized files are rejected before a doomed upload.
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
// Where the read-aloud player remembers how far it got (see _savePosition):
// leaving the page kills the browser's speech, and this is what lets the
// thread offer to carry on from that sentence instead of from the top.
const POSITION_KEY = 'retinue-voice-position';
const POSITION_TTL_MS = 7 * 24 * 60 * 60 * 1000;
// Types the gateway will serve with `Content-Disposition: inline`, i.e. that the
// browser shows in place instead of saving. Mirrors _INLINE_SAFE_TYPES in
// web-gateway.py — offering "view" for anything else would just download it.
const INLINE_SAFE_TYPES = new Set([
  'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif',
  'application/pdf', 'text/plain',
]);

class RetinueConversations extends HTMLElement {
  constructor() {
    super();
    this._threads = [];     // list summaries
    this._active = null;    // id of the open thread, or null for the list view
    this._thread = null;    // full active thread
    this._composing = false; // true while the "new thread" composer is open
    this._timer = null;
    this._busy = false;
    this._drafts = {};       // map of conversation id to draft text. 'composer' is used for the new thread composer.
    this._outFiles = {};     // map of conversation id to pending outgoing attachments
    this._attachError = '';  // last attach error (e.g. file too big), shown by the composer
    this._focusNext = false; // focus the input after the next render (view opened)
    this._hadFocus = false;  // input had focus before the current re-render
    this._listSig = '';
    this._threadSig = '';
    this._full = false;      // full mode: dedicated all-conversations page
    this._scope = 'active';  // full-mode thread filter: active|archived|edits|cowork
    this._composeProject = null;      // project URI the composer is about, if any
    this._composeProjectTitle = '';   // its display title (for the chip)
    this._pushDepth = 0;     // history entries we pushed and have not unwound
    // Model picker: which model answers a thread. The offered list comes from
    // the gateway (single source of truth); '' means the gateway default. The
    // choice is per-thread — pickable at creation and switchable mid-thread,
    // effective next turn (each turn is a fresh `claude -p`). _composeModel holds
    // the pending choice for the new-thread composer.
    this._models = [];
    this._composeModel = '';
    // Voice: record a message (server transcribes) and speak replies back.
    this._recState = 'idle'; // idle | recording | transcribing
    this._recChunks = [];
    this._mediaRecorder = null;
    this._recStream = null;
    this._recTarget = null;  // thread pinned at record-start (dictation target)
    this._recIntent = null;  // what to do with the transcript: 'review' | 'send'
    this._recAborted = false; // recording was discarded via the abort button
    // In-flight dictation jobs, keyed like _drafts (a thread id, or 'composer'
    // for the new-thread composer). Each value is {sending, phase} and owns
    // that one view's input row until the job completes — every other
    // conversation keeps its normal row, so text and voice stay usable there
    // while a transcription runs in the background.
    this._voiceJobs = {};
    // Transcription errors per target view, surfaced by that view's composer —
    // a background job's failure must not pop up in whatever view is open.
    this._voiceErrors = {};
    // Live waveform on the recording row's canvas (shared renderer, voice.js).
    this._wave = new Waveform(this);
    this._autoplay = false;  // speak Ara's replies as they arrive
    try { this._autoplay = localStorage.getItem('retinue-voice-autoplay') === '1'; } catch (_e) { /* ignore */ }
    this._spoken = {};       // per-thread set of message ts already voiced/seen
    this._autoReady = {};    // per-thread: initial history marked, future msgs autoplay
    // The read-aloud player (speech.js): one message at a time, cut into
    // sentence pieces, with pause and seek. _playing names the loaded message
    // (thread id, message ts, sender label, thread title) so the player bar
    // can say what it is reading from any view; _scrubbing is set while the
    // user drags the position slider, so a progress tick does not yank it.
    this._reader = new Reader();
    this._reader.onprogress = (ev) => this._onReaderProgress(ev);
    this._playing = null;
    this._scrubbing = false;
    this._onVisible = () => {
      if (document.visibilityState === 'visible') this._reader.resync();
    };
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._full = this.hasAttribute('full');
    // List rows vs reflowing tiles — a per-device choice (see base.js).
    this._view = viewPref('conversations');
    // Deep link: #conversation-<id> opens that thread (used by agent push
    // URLs); #new opens the composer.
    const m = CONV_HASH_RE.exec(location.hash || '');
    const cm = COMPOSER_HASH_RE.exec(location.hash || '');
    if (m) this._active = m[1];
    else if (cm) { this._composing = true; this._setComposeProject(cm[1]); }
    this._onPop = () => this._syncFromLocation();
    window.addEventListener('popstate', this._onPop);
    // Also on hashchange: tapping a push notification navigates an
    // already-open window to #conversation-<id>, and relying on popstate alone
    // for that fragment change is implementation-dependent.
    window.addEventListener('hashchange', this._onPop);
    // A backgrounded engine may drop the utterance it was speaking without a
    // word; on return the reader checks and picks the sentence up again.
    document.addEventListener('visibilitychange', this._onVisible);
    // Crossing the layout breakpoint changes how many threads fit (see
    // _shownThreads), so re-render when it flips.
    this._offFrame = onFrameChange(() => { if (!this._full) this.render(); });
    this.render();
    this.refresh();
    this._loadModels();
    this._timer = setInterval(() => this.refresh(), POLL_MS);
  }

  // Fetch the offered model list once. A failure (or a single-model list) simply
  // leaves the picker hidden — conversations work exactly as before.
  async _loadModels() {
    try {
      const res = await fetch('/conversation-models', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      if (Array.isArray(data.models)) {
        this._models = data.models;
        this.render();
      }
    } catch (_err) { /* picker stays hidden */ }
  }

  _modelLabel(id) {
    const m = (this._models || []).find((x) => x.id === (id || ''));
    return m ? m.label : '';
  }

  disconnectedCallback() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
    if (this._onPop) {
      window.removeEventListener('popstate', this._onPop);
      window.removeEventListener('hashchange', this._onPop);
    }
    this._onPop = null;
    document.removeEventListener('visibilitychange', this._onVisible);
    if (this._offFrame) this._offFrame();
    this._offFrame = null;
    this._stopRecording();
    this._wave.stop();
    this._stopStream();
    // Silence the engine but keep the saved position: the element goes away
    // with the page, and the next visit offers to resume.
    this._reader.stop();
  }

  // Bring the view in line with the address bar after the browser has already
  // moved through history (back gesture, forward, deep link): adjust the view,
  // never push — pushing here would fight the history stack.
  _syncFromLocation() {
    const hash = location.hash || '';
    // At most one view entry is ever on the stack (the composer's is replaced
    // by the thread's on send), so presence of a hash is the whole state.
    this._pushDepth = hash ? 1 : 0;
    const m = CONV_HASH_RE.exec(hash);
    if (m) {
      if (this._active !== m[1]) this._showThread(m[1]);
      return;
    }
    const cm = COMPOSER_HASH_RE.exec(hash);
    if (cm) {
      this._setComposeProject(cm[1]);
      if (!this._composing) this._showComposer();
      return;
    }
    if (this._active || this._composing) this._showList();
  }

  // Parse the composer hash's optional query (project=…&title=…) into the
  // pending project link. Called with the raw query part, or undefined.
  _setComposeProject(query) {
    this._composeProject = null;
    this._composeProjectTitle = '';
    if (!query) return;
    try {
      const params = new URLSearchParams(query);
      this._composeProject = params.get('project') || null;
      this._composeProjectTitle = params.get('title') || '';
    } catch (_e) { /* malformed hash — plain composer */ }
  }

  get heading() { return this.getAttribute('heading') || 'Conversations'; }

  // True while a page reload would lose in-memory user input — a composer or
  // reply draft (kept per thread in _drafts even after leaving the view) or
  // files picked for upload. components/update.js consults this before
  // auto-reloading into a freshly activated shell version.
  get dirty() {
    const hasDraft = Object.values(this._drafts || {}).some((t) => t && t.trim());
    const hasFiles = Object.values(this._outFiles || {}).some((fs) => fs && fs.length);
    return hasDraft || hasFiles;
  }

  // In full mode the filter can request the archived scope or either of the
  // normally hidden kinds — project edit-command threads and the Ask-Ara MCP
  // connector's cowork audit threads; otherwise we list active chat threads —
  // the default the dashboard card and agents expect.
  _listUrl() {
    if (this._full && this._scope === 'archived') return `${LIST_URL}?archived=1`;
    if (this._full && this._scope === 'edits') return `${LIST_URL}?all=1&kind=edit`;
    if (this._full && this._scope === 'cowork') return `${LIST_URL}?all=1&kind=cowork`;
    return LIST_URL;
  }

  // How many threads the list shows. The full page shows them all; so does the
  // dashboard card in the wide layout, where the list is a scroll box of its own
  // and a cap would only leave the column half empty. Only the phone layout,
  // where every row lengthens the page, keeps the cap.
  _shownThreads() {
    return (this._full || isWideFrame())
      ? this._threads : this._threads.slice(0, MAX_CARD_THREADS);
  }

  async refresh() {
    try {
      const res = await fetch(this._listUrl(), { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      this._threads = Array.isArray(data.conversations) ? data.conversations : [];
      if (this._active) await this._loadThread(this._active);
      // Partial update only: never replace the input form (would cancel the
      // browser dictation session) or the scroll container (would jump to top).
      this._partialUpdate();
    } catch (_err) {
      // Offline or gateway down: keep the last rendered state.
    }
  }

  async _loadThread(id) {
    try {
      const res = await fetch(`/conversations/${id}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      this._thread = await res.json();
      if (this._thread.unread) this._markRead(id);
      this._maybeAutoplay(this._thread);
      this._restorePosition(this._thread);
    } catch (_err) {
      // keep previous thread state
    }
  }

  // Apply server-driven updates (new messages, badge counts, thread list)
  // without rebuilding the entire shadow DOM. This keeps the input element
  // alive so dictation, IME composition, focus, and selection survive a poll,
  // and keeps the thread's scroll position stable.
  _partialUpdate() {
    const root = this.shadowRoot;
    if (!root) return;
    // If we're in a structurally different view than last full render, fall
    // back to a full render so the right widgets exist to update in place.
    const mode = this._active ? 'thread' : (this._composing ? 'composer' : 'list');
    if (mode !== this._lastMode) { this.render(); return; }

    // Header badge
    const hdr = root.querySelector('header');
    if (hdr) {
      const n = this._unreadCount();
      let badge = hdr.querySelector('.badge');
      if (n && !badge) {
        // After the heading, not at the end — the view toggle sits at the far
        // right of the header and the badge belongs beside the title.
        hdr.querySelector('h2').insertAdjacentHTML('afterend', `<span class="badge">${n}</span>`);
      } else if (n && badge) {
        if (badge.textContent !== String(n)) badge.textContent = String(n);
      } else if (!n && badge) {
        badge.remove();
      }
    }

    if (mode === 'list') {
      const tabsEl = root.querySelector('.tabs');
      if (tabsEl) {
        const sig = this._listSignature();
        if (sig !== this._listSig) {
          tabsEl.innerHTML = this._tabsHtml() + this._emptyHtml();
          this._listSig = sig;
          const allLink = root.querySelector('.all-link');
          if (allLink && !this._full) allLink.innerHTML = this._allLinkLabel();
          tabsEl.querySelectorAll('[data-open]').forEach((el) =>
            el.addEventListener('click', () => this._openThread(el.getAttribute('data-open'))));
        }
      }
    } else if (mode === 'thread') {
      const t = this._thread;
      if (!t) return;
      // A cold deep link renders the thread view before the thread has loaded,
      // so the message container doesn't exist yet — only a full render can
      // introduce it (and its composer) once the data is here.
      if (!root.querySelector('.thread')) { this.render(); return; }
      const titleEl = root.querySelector('[data-title]');
      if (titleEl) {
        const want = t.title || 'Conversation';
        if (titleEl.textContent !== want) titleEl.textContent = want;
      }
      const threadEl = root.querySelector('.thread');
      if (threadEl) {
        const sig = this._threadSignature(t);
        if (sig !== this._threadSig) {
          // Preserve scroll position. Only auto-stick to bottom when the user
          // was already near the bottom before new content arrived; otherwise a
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
      }
    }
  }

  async _markRead(id) {
    try { await fetch(`/conversations/${id}/read`, { method: 'POST' }); } catch (_err) { /* ignore */ }
  }

  async _archive(id, archived) {
    if (this._busy) return;
    this._busy = true;
    try {
      const res = await fetch(`/conversations/${id}/${archived ? 'archive' : 'unarchive'}`,
        { method: 'POST' });
      if (!res.ok) throw new Error(String(res.status));
      // Reflect it locally so the thread leaves/joins the current scope at once.
      if (this._thread) this._thread.archived = archived;
    } catch (_err) {
      // keep the thread open; a later poll will reconcile state
    } finally {
      this._busy = false;
      this._openList();
      this.refresh();
    }
  }

  // Switch the full-page thread filter and reload that scope.
  _setScope(scope) {
    if (this._scope === scope) return;
    this._scope = scope;
    this._threads = [];
    this.render();
    this.refresh();
  }

  // `targetOverride` sends to a specific thread ('composer' for a new thread)
  // regardless of what's open — used by send-intent dictation, where the
  // user may have navigated away while transcription ran. Omitted for normal
  // sends, which go to the open view.
  async _send(text, targetOverride) {
    const override = targetOverride != null;
    const sendToComposer = override ? (targetOverride === 'composer') : this._composing;
    const sendToThread = override
      ? (targetOverride && targetOverride !== 'composer' ? targetOverride : null)
      : this._active;
    const draftKey = sendToThread || (sendToComposer ? 'composer' : '');
    const currentOutFiles = this._outFiles[draftKey] || [];
    // A message needs text or at least one attachment.
    if (this._busy || (!text.trim() && !currentOutFiles.length)) return;
    // Whether this send targets the view the user is currently looking at. When
    // false (a dictation sent into a thread the user has navigated away from), we
    // must not hijack their view with the result.
    const affectsView = sendToThread ? (this._active === sendToThread)
      : (sendToComposer && this._composing);
    this._busy = true;
    try {
      const body = { message: text };
      // A composer opened from a project page links the new thread to that
      // project, so Ara starts from the project file's current state.
      if (sendToComposer && this._composeProject) {
        body.project = this._composeProject;
        if (this._composeProjectTitle) body.project_title = this._composeProjectTitle;
      }
      // Carry the composer's model choice onto the new thread ('' = default,
      // which the server simply leaves unset).
      if (sendToComposer && this._composeModel) body.model = this._composeModel;
      if (currentOutFiles.length) {
        body.attachments = currentOutFiles.map((f) => ({
          filename: f.name, content_type: f.type, data: f.data,
        }));
      }
      const url = sendToThread ? `/conversations/${sendToThread}/messages` : LIST_URL;
      const res = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(String(res.status));
      const conv = await res.json();
      // Clear the draft/attachments for whichever target we just sent.
      this._drafts[draftKey] = '';
      this._outFiles[draftKey] = [];
      this._attachError = '';
      if (sendToComposer) {
        // A brand-new thread. Only bring the user onto it if they were still on
        // the composer; otherwise leave their current view untouched.
        this._drafts['composer'] = '';
        this._outFiles['composer'] = [];
        this._composeModel = '';  // consumed by this thread; reset for the next one
        if (affectsView) {
          // A thread opened from the composer reuses the composer's history
          // entry, so back still lands on the list rather than the (now gone)
          // composer.
          history.replaceState(null, '', `#conversation-${conv.id}`);
          this._active = conv.id;
          this._thread = conv;
          this._composing = false;
          this._setComposeProject(undefined); // link consumed by this thread
        }
      } else if (affectsView) {
        this._thread = conv;
      }
    } catch (_err) {
      // surface a soft failure inline by leaving the input; re-render shows state
    } finally {
      this._busy = false;
      // Re-render only when this send concerns the view on screen: a
      // background dictation-send must not rebuild (and so interrupt) a
      // conversation the user is meanwhile typing or dictating in — the list
      // refresh below reconciles previews and badges on its own.
      if (affectsView) this.render();
      this.refresh();
    }
  }

  // _open* are user intents: they move the history stack, and the matching
  // _show* brings the view along. _show* alone mutates view state — that is
  // what _syncFromLocation() calls when the browser moved history for us.

  _openThread(id) {
    if (this._active === id) return;
    history.pushState(null, '', `#conversation-${id}`);
    this._pushDepth += 1;
    this._showThread(id);
  }

  _showThread(id) {
    this._finishRecordingOnLeave();
    this._active = id;
    this._composing = false;
    this._thread = null;
    // Do not clear this._drafts[id] here. Let the user's previously entered text
    // remain so it isn't lost if they navigate away and then back.
    // this._outFiles[id] is preserved
    this._attachError = '';
    this._focusNext = true;
    this.render();
    this._loadThread(id).then(() => this.render());
  }

  // Leaving a thread: if we pushed the entry, unwind it, so the back gesture
  // and the Back button agree and the stack does not grow on every open/close.
  // popstate then runs _showList(). If the hash came from a deep link (we never
  // pushed), going back would leave the PWA — drop the hash in place instead.
  _openList() {
    if (this._pushDepth > 0) { history.back(); return; }
    if (location.hash) history.replaceState(null, '', location.pathname);
    this._showList();
  }

  _showList() {
    this._finishRecordingOnLeave();
    this._active = null;
    this._composing = false;
    this._thread = null;
    // this._drafts is preserved
    // this._outFiles is preserved
    this._attachError = '';
    this.render();
  }

  _openComposer() {
    history.pushState(null, '', COMPOSER_HASH);
    this._pushDepth += 1;
    this._setComposeProject(undefined); // the "+ New" button starts a plain thread
    this._showComposer();
  }

  _showComposer() {
    this._finishRecordingOnLeave();
    this._active = null;
    this._thread = null;
    this._composing = true;
    // this._drafts is preserved
    // this._outFiles is preserved
    this._attachError = '';
    this._focusNext = true;
    this.render();
  }

  render() {
    // Remember whether our input had focus so a background-poll re-render can
    // restore it (and not steal focus when the user wasn't typing).
    const prev = this.shadowRoot && this.shadowRoot.querySelector('[data-form] textarea');
    this._hadFocus = !!(prev && this.shadowRoot.activeElement === prev);
    const mode = this._active ? 'thread' : (this._composing ? 'composer' : 'list');
    // Reflect the view on the host so the page can react (styles.css hides the
    // greeting and app dock while a thread or the composer is open).
    this.setAttribute('data-view', mode);
    const body = this._active ? this._threadView()
      : this._composing ? this._composerView()
      : this._listView();
    // Thread and composer views carry their own top bar (back button + title),
    // so the card header would only repeat it — render it for the list alone.
    const header = mode === 'list'
      ? `<header><h2>${esc(this.heading)}</h2>` +
        `${this._unreadCount() ? `<span class="badge">${this._unreadCount()}</span>` : ''}` +
        `${viewToggleHtml(this._view)}</header>`
      : '';
    this.shadowRoot.innerHTML = `<style>${CSS}${VIEW_TOGGLE_CSS}${VOICE_CSS}${MD_CSS}</style>` +
      `<section class="card">${header}<div class="content">${body}</div></section>`;
    this._lastMode = mode;
    this._listSig = this._lastMode === 'list' ? this._listSignature() : '';
    this._threadSig = (this._lastMode === 'thread' && this._thread) ? this._threadSignature(this._thread) : '';
    this._wire();
    // After a full render in thread view, scroll to bottom so the latest
    // message is visible (matches typical chat-app behaviour on open).
    if (this._lastMode === 'thread') {
      const threadEl = this.shadowRoot.querySelector('.thread');
      if (threadEl) threadEl.scrollTop = threadEl.scrollHeight;
    }
  }

  _unreadCount() {
    return this._threads.filter((t) => t.unread).length;
  }

  _listSignature() {
    return JSON.stringify(this._threads.map((t) => [
      t.id, t.title, t.initiator, t.updated, !!t.unread, !!t.pending, t.last_preview,
      t.kind || '', t.project_title || '',
    ]));
  }

  _threadSignature(t) {
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

  _listView() {
    // A new thread is always an active chat, so the composer would be
    // confusing while the Archived or Edits filter is showing — hide it there.
    const newBtn = (this._full && this._scope !== 'active')
      ? '' : '<button class="new" data-new>+ New conversation with Ara</button>';
    // The tabs area takes all remaining height and scrolls; the New button and
    // page link stay pinned at the bottom, within thumb reach.
    return this._filterHtml() +
      `<div class="tabs${this._view === 'list' ? ' as-list' : ''}">` +
      `${this._tabsHtml()}${this._emptyHtml()}</div>` +
      `<div class="list-foot">${newBtn}${this._footerHtml()}</div>` +
      this._playerHostHtml();
  }

  // Active/Archived/Edits/Cowork switch — only in the dedicated full-page view.
  // The last two filters are where the normally hidden kinds remain reachable:
  // project edit-command threads, and the audit threads the Ask-Ara MCP
  // connector writes for every exchange with an outside Claude session.
  _filterHtml() {
    if (!this._full) return '';
    const tab = (scope, label) =>
      `<button class="filter-tab${this._scope === scope ? ' on' : ''}" data-scope="${scope}">${label}</button>`;
    return `<div class="filter">${tab('active', 'Active')}${tab('archived', 'Archived')}` +
      `${tab('edits', 'Edits')}${tab('cowork', 'Cowork')}</div>`;
  }

  _emptyHtml() {
    if (this._threads.length) return '';
    const msg = (this._full && this._scope === 'archived')
      ? 'No archived conversations.'
      : (this._full && this._scope === 'edits')
        ? 'No edit commands yet. Dictate or type one on a project page.'
        : (this._full && this._scope === 'cowork')
          ? 'No cowork sessions yet. These appear when an outside Claude session asks Ara something.'
          : 'No conversations yet.';
    return `<div class="empty"><span class="e-ico" aria-hidden="true">&#x1F4AC;</span><p>${msg}</p></div>`;
  }

  // The card links out to the full page; the full page links back home.
  _footerHtml() {
    if (this._full) return '<a class="all-link" href="/">&larr; Back to dashboard</a>';
    return `<a class="all-link" href="/conversations.html">${this._allLinkLabel()}</a>`;
  }

  _allLinkLabel() {
    // The count is a "there is more over there" hint, so it only earns its place
    // while the list is actually truncated.
    const more = this._threads.length > this._shownThreads().length
      ? ` (${this._threads.length})` : '';
    return `All conversations${more} &rarr;`;
  }

  _tabsHtml() {
    return this._shownThreads().map((t) => {
      const meta = [
        t.initiator === 'agent' ? 'Retinue' : 'You',
        t.updated ? fmtAge(t.updated) : '',
        t.archived ? 'archived' : '',
        t.project_title || '',
      ].filter(Boolean).join(' · ');
      // Edit-command threads only ever appear under the Edits filter (or on
      // their project's page) — badge them so their nature is obvious there.
      const editTag = t.kind === 'edit' ? '<span class="tag-edit">edit</span>' : '';
      return `<button class="tab${t.unread ? ' unread' : ''}" data-open="${esc(t.id)}">` +
        `<span class="t-title">${t.unread ? '<span class="dot"></span>' : ''}` +
        editTag +
        `<span class="t-name">${esc(t.title || 'Conversation')}</span></span>` +
        `<small class="t-meta">${esc(meta)}</small>` +
        (t.last_preview ? `<small class="t-prev">${esc(t.last_preview)}</small>` : '') +
        `</button>`;
    }).join('');
  }

  _backBtnHtml() {
    return '<button class="back" data-back aria-label="Back">&#8249;</button>';
  }

  // The model dropdown. Governs Ara's own turn only (dispatched subagents keep
  // their own models) — the title says so. Hidden unless the gateway offers more
  // than one model, so a single-model deployment sees no clutter. `selected` is
  // the currently-chosen id; '' means the thread rides the gateway default,
  // which the list carries not as its own row but as a `default: true` flag on
  // the concrete entry that default runs on — show that entry as selected. Only
  // when the gateway could not name its default (no flagged entry) does a
  // hidden, unpickable placeholder keep the select from claiming a concrete
  // model it is not running.
  // `wide` renders the roomy composer form: a visible "Model" caption and an
  // untruncated select, instead of the bar's compact gear + capped-width one.
  _modelPickerHtml(selected, { wide = false } = {}) {
    const models = this._models || [];
    if (models.length < 2) return '';
    let sel = selected || '';
    if (!models.some((m) => m.id === sel)) {
      const def = models.find((m) => m.default);
      sel = def ? def.id : '';
    }
    const placeholder = models.some((m) => m.id === sel) ? ''
      : '<option value="" hidden selected>Default</option>';
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

  _composerView() {
    // Coming from a project page, show what the new thread will be about.
    const projectChip = this._composeProject
      ? `<div class="about-chip">About: ${esc(this._composeProjectTitle || this._composeProject)}</div>`
      : '';
    const hint = this._composeProject
      ? `<p>Ask Ara about this project &mdash; she reads its current state first.</p>`
      : `<p>Ask Ara anything &mdash; she picks it up with full context.</p>`;
    // The picker sits in the body as a labeled, full-width row — cramped into
    // the top bar it truncated its labels and was easy to miss, and picking
    // the model is exactly the choice to make before the first message goes
    // out (it can still be switched later from the thread bar).
    return `<div class="thread-bar">${this._backBtnHtml()}` +
      `<span class="bar-title">New conversation</span></div>` +
      projectChip +
      `<div class="empty"><span class="e-ico" aria-hidden="true">&#x1F4AC;</span>` +
      hint +
      this._modelPickerHtml(this._composeModel, { wide: true }) + `</div>` +
      this._playerHostHtml() +
      this._inputRow('Ask Ara something …');
  }

  _threadView() {
    const t = this._thread;
    if (!t) {
      return `<div class="thread-bar">${this._backBtnHtml()}` +
        `<span class="bar-title muted">&#8230;</span></div>`;
    }
    const archiveBtn = t.archived
      ? '<button class="pill" data-unarchive>Unarchive</button>'
      : '<button class="pill" data-archive>Archive</button>';
    const autoBtn = speechAvailable()
      ? `<button class="iconbtn${this._autoplay ? ' on' : ''}" data-autoplay ` +
        `title="Speak Ara's replies as they arrive" aria-label="Speak replies as they arrive" ` +
        `aria-pressed="${this._autoplay}">${this._autoplay ? '\u{1F50A}' : '\u{1F507}'}</button>`
      : '';
    return `<div class="thread-bar">${this._backBtnHtml()}` +
      `<span class="bar-title" data-title>${esc(t.title || 'Conversation')}</span>` +
      `<span class="bar-actions">${this._modelPickerHtml(t.model)}${autoBtn}${archiveBtn}</span></div>` +
      `<div class="thread">${this._messagesHtml(t)}</div>` +
      this._playerHostHtml() +
      this._inputRow('Reply …');
  }

  _messagesHtml(t) {
    const canSpeak = speechAvailable();
    const msgs = (t.messages || []).map((m, idx) => {
      const cls = m.role === 'user' ? 'me' : (m.role === 'agent' ? 'agent' : 'ara');
      const reading = this._isLoaded(t, m) ? ' reading' : '';
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
  // message, its timestamp. Each piece is optional — older messages predating
  // this metadata simply omit what they lack. Rendered as middot-separated
  // muted text so it reads as one quiet line.
  _metaHtml(m) {
    const bits = [];
    if (m.model_name) bits.push(`<span class="m-model">${esc(m.model_name)}</span>`);
    if (typeof m.cost_usd === 'number' && isFinite(m.cost_usd)) {
      bits.push(`<span class="m-cost" title="Approximate list-price cost — not the subscription bill">` +
        `~$${this._fmtCost(m.cost_usd)}</span>`);
    }
    if (m.ts) {
      bits.push(`<time class="m-ts" datetime="${esc(m.ts)}" title="${esc(m.ts)}">` +
        `${esc(fmtAge(m.ts))}</time>`);
    }
    if (!bits.length) return '';
    return `<small class="msg-meta">${bits.join('<span class="m-sep">·</span>')}</small>`;
  }

  // Cost with enough precision to stay meaningful for cheap turns: sub-cent
  // values get more decimals so they don't collapse to "~$0.00".
  _fmtCost(v) {
    const c = Math.abs(v);
    if (c === 0) return '0';
    if (c < 0.01) return c.toFixed(4);
    if (c < 1) return c.toFixed(3);
    return c.toFixed(2);
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
      const size = this._fmtSize(a.size);
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

  _fmtSize(n) {
    if (!Number.isFinite(n) || n <= 0) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  // Render a message body with the shared Markdown renderer (markdown.js), so
  // bubbles and project pages show the same text the same way. Blockquotes —
  // how Ara offers ready-to-send drafts — keep their copy button: it puts the
  // clean, un-prefixed text on the clipboard so the user can paste it straight
  // into WhatsApp/e-mail.
  _renderBubble(text) {
    return renderMarkdown(text, {
      quote: (raw, inner) =>
        `<blockquote class="md-quote quote"><div class="q-text">${inner}</div>` +
        `<button class="copy" type="button" data-copy="${esc(raw)}">Copy</button>` +
        `</blockquote>`,
      // Fenced code blocks get the same copy affordance as blockquotes — Ara
      // hands out ready-to-paste prompts as code blocks too. The delegated
      // `.copy` click handler on the thread covers this button as well.
      code: (raw, _lang, inner) =>
        `<div class="code-wrap">${inner}` +
        `<button class="copy code-copy" type="button" data-copy="${esc(raw)}">Copy</button>` +
        `</div>`,
    });
  }

  async _copyToClipboard(btn) {
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
        (this.shadowRoot || document.body).appendChild(ta);
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

  // Drop a chip's prefill text into the composer for review. Deliberately does
  // NOT send: the user reads (and can edit) it, then taps Send — same contract
  // as a dictation transcribed for review. APPENDS to whatever the user has already
  // typed rather than replacing it (a chip augments the draft, it never wipes
  // work in progress) — the same append semantics as a dictation. Persists to
  // the draft so a background-poll re-render doesn't wipe it, then re-renders to
  // show the text and focus the field with the caret at the end.
  _fillComposer(text) {
    const draftKey = this._active || (this._composing ? 'composer' : '');
    if (!draftKey) return;
    this._appendToDraft(text);
    this._focusNext = true;
    this.render();
  }

  _pendingStartedAt(t) {
    return t.pending_since || t.updated || t.created || null;
  }

  _pendingAgeSeconds(t) {
    const started = this._pendingStartedAt(t);
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

  _inputRow(placeholder) {
    const disabled = this._busy ? 'disabled' : '';
    const draftKey = this._active || (this._composing ? 'composer' : '');
    const currentOutFiles = draftKey ? (this._outFiles[draftKey] || []) : [];
    const chips = currentOutFiles.map((f, i) =>
      `<span class="chip"><span class="c-name">${esc(f.name)}</span>` +
      `<span class="c-size">${esc(this._fmtSize(f.size))}</span>` +
      `<button type="button" class="c-x" data-rmfile="${i}" aria-label="Remove attachment" ${disabled}>&times;</button></span>`
    ).join('');
    const chipRow = currentOutFiles.length ? `<div class="chips">${chips}</div>` : '';
    const voiceErr = draftKey ? (this._voiceErrors[draftKey] || '') : '';
    const errText = this._attachError || voiceErr;
    const errRow = errText ? `<div class="attach-err">${esc(errText)}</div>` : '';
    const currentDraft = draftKey ? (this._drafts[draftKey] || '') : '';
    // The voice flow owns this one view's input row: a live waveform with its
    // own controls while recording, then a status line while this view's
    // dictation job is transcribed (and, on the send path, sent). The textarea
    // stays out of the DOM for the entire flow, so the phone keyboard never
    // pops up mid-dictation. Other views are untouched — their rows render
    // normally below, and they can dictate concurrently.
    if (this._recState === 'recording' && this._recTarget === draftKey) {
      return `<div class="composer">` + chipRow + errRow + recordingRowHtml() + `</div>`;
    }
    const job = draftKey ? this._voiceJobs[draftKey] : null;
    if (job) {
      const label = job.phase === 'sending' ? 'Sending …'
        : (job.sending ? 'Transcribing & sending …' : 'Transcribing …');
      return `<div class="composer">` + chipRow + errRow + statusRowHtml(label) + `</div>`;
    }
    // Only one live recording at a time — but a mere background transcription
    // does not lock the mic here.
    const micLabel = '\u{1F3A4}';
    const micTitle = 'Record a voice message';
    const micDisabled = (this._busy || this._recState !== 'idle') ? 'disabled' : '';
    const micBtn = canRecord()
      ? `<button type="button" class="mic" ` +
        `data-mic title="${micTitle}" aria-label="${micTitle}" ${micDisabled}>${micLabel}</button>`
      : '';
    // A lean row keeps the width for the text field: mic on the left, the
    // attach control tucked inside the field, send on the right.
    return `<div class="composer">` + chipRow + errRow +
      `<form class="row" data-form>` + micBtn +
      `<div class="field">` +
      `<textarea rows="1" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}" autocomplete="off" ${disabled}>` +
      `${esc(currentDraft)}</textarea>` +
      `<label class="clip" title="Attach a file" aria-label="Attach a file">` +
      `<input type="file" multiple hidden data-file ${disabled}>` +
      `<span aria-hidden="true">\u{1F4CE}</span></label>` +
      `</div>` +
      `<button type="submit" title="Send" aria-label="Send" ${disabled}>➤</button></form></div>`;
  }

  // Read picked files into base64 (chunked, so large files don't overflow the
  // String.fromCharCode call stack) and stage them as pending attachments.
  async _addFiles(fileList) {
    this._attachError = '';
    const draftKey = this._active || (this._composing ? 'composer' : '');
    if (!draftKey) return;
    if (!this._outFiles[draftKey]) this._outFiles[draftKey] = [];
    for (const file of Array.from(fileList || [])) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        this._attachError = `"${file.name}" is too large (max ${this._fmtSize(MAX_ATTACHMENT_BYTES)}).`;
        continue;
      }
      try {
        const buf = new Uint8Array(await file.arrayBuffer());
        let binary = '';
        for (let i = 0; i < buf.length; i += 0x8000) {
          binary += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        }
        this._outFiles[draftKey].push({
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
    const draftKey = this._active || (this._composing ? 'composer' : '');
    if (!draftKey || !this._outFiles[draftKey]) return;
    this._outFiles[draftKey].splice(index, 1);
    this._attachError = '';
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
    this._reader.pause();
    const viewKey = this._viewKey();
    // The status row hides the mic while this view's own job runs, but guard
    // anyway: one dictation job per conversation at a time.
    if (viewKey && this._voiceJobs[viewKey]) return;
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
      // The recording belongs to the view it runs in: that is where its
      // controls live, and navigating away finishes it like a tap on the green
      // check (see _finishRecordingOnLeave). So the view noted here is by
      // construction also the view where the check/send tap — explicit or
      // implicit — happens, and that is where the transcript lands.
      // '' means the "new thread" composer.
      this._recTarget = viewKey;
      delete this._voiceErrors[viewKey];
      const mr = new MediaRecorder(stream);
      this._mediaRecorder = mr;
      mr.addEventListener('dataavailable', (e) => {
        if (e.data && e.data.size) this._recChunks.push(e.data);
      });
      mr.addEventListener('stop', () => this._onRecordingStopped());
      mr.start();
      this._recState = 'recording';
      this._attachError = '';
      this.render();
      this._wave.start(stream);
    } catch (_err) {
      this._recState = 'idle';
      this._recTarget = null;
      this._attachError = 'Microphone access was denied.';
      this._stopStream();
      this.render();
    }
  }

  // The view key drafts/jobs are filed under: the open thread id, or
  // 'composer' for the new-thread composer, or '' on the list.
  _viewKey() {
    return this._active || (this._composing ? 'composer' : '');
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

  // Leaving the view that hosts a live recording is the same as tapping the
  // green check: the recording stops, and its transcript lands in the draft of
  // the conversation the user just left — waiting there, reviewed on return.
  _finishRecordingOnLeave() {
    this._finishRecording('review');
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
    // The view the recording ran in — where the check/send tap (or the
    // navigation that acted as one) happened, and where the transcript lands.
    const target = this._recTarget != null ? this._recTarget : this._viewKey();
    this._recTarget = null;
    this._recState = 'idle';
    if (aborted || !chunks.length) {
      this.render();
      return;
    }
    const blob = new Blob(chunks, { type });
    // From here on the dictation is a background job of its target view alone:
    // the recorder is free again, other conversations keep their normal input
    // row (text and voice), and only the target's row shows the status line.
    if (target) this._voiceJobs[target] = { sending: intent === 'send', phase: 'transcribing' };
    this.render();
    let toSend = '';
    try {
      // The target thread is context for the cleanup pass: it is what tells the
      // model which names and topics this dictation is likely to be about.
      // 'composer' is a UI key, not a thread id — only a real thread id is sent.
      const q = (target && target !== 'composer')
        ? `?thread=${encodeURIComponent(target)}` : '';
      const res = await fetch(`/conversations/transcribe${q}`, {
        method: 'POST',
        headers: { 'Content-Type': blob.type || 'application/octet-stream' },
        body: blob,
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const text = ((data && data.text) || '').trim();
      if (text) {
        this._appendToDraft(text, target);
        // Send the whole draft, so anything typed before dictating comes along.
        if (intent === 'send' && target) toSend = this._drafts[target] || '';
      } else {
        this._voiceErrors[target] = 'No speech was detected in the recording.';
      }
    } catch (_err) {
      this._voiceErrors[target] = "Couldn't transcribe the recording. Please try again.";
    }
    if (toSend) {
      // Send path: the status row stays in place of the textarea until the
      // send completes, so the keyboard never appears. _send() re-renders only
      // when the user is looking at the target; on failure it leaves the
      // draft in place, which then shows up (unfocused) for a manual retry.
      if (target) this._voiceJobs[target].phase = 'sending';
      await this._send(toSend, target);
    }
    delete this._voiceJobs[target];
    // Completion must not interrupt whatever the user is doing now: only when
    // they are still looking at the target view is it re-rendered — and only
    // the deliberate review flow pulls up the keyboard. A background job's
    // result just sits in that conversation's draft (or error slot) until the
    // user returns to it.
    if (this._viewKey() === target) {
      this._focusNext = intent === 'review';
      this.render();
    }
  }

  // Model dropdown changed. In the composer it just holds the choice for the
  // thread we're about to create. In an open thread it's persisted server-side
  // right away (takes effect on the next turn) so a page reload keeps it.
  async _onModelChange(value) {
    const model = value || '';
    if (this._composing || !this._active) {
      this._composeModel = model;
      return;
    }
    // Optimistic: reflect it locally, then persist. On failure, re-render
    // restores the server's value on the next poll.
    if (this._thread) this._thread.model = model;
    try {
      await fetch(`/conversations/${this._active}/model`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
    } catch (_err) { /* next poll re-syncs the real value */ }
    this.refresh();
  }

  // `target` (a thread id, or 'composer') pins where the text lands; omitted, it
  // falls back to the open view. Dictation passes the thread captured at
  // record-start so the transcript lands there even if the user navigated away.
  _appendToDraft(text, target) {
    const draftKey = target != null
      ? target
      : (this._active || (this._composing ? 'composer' : ''));
    if (!draftKey) return;
    const cur = this._drafts[draftKey] || '';
    this._drafts[draftKey] = cur ? `${cur.replace(/\s*$/, '')} ${text}` : text;
  }

  // ── Voice output: the read-aloud player over Ara's replies ────────────────
  // One message at a time is loaded into the shared Reader (speech.js), which
  // speaks it sentence by sentence and owns pause, seek and skip. The player
  // bar (.player-host, present in every view) shows what is being read and
  // where; it follows the user into the list, so leaving the thread does not
  // end the reading. Leaving the PAGE does — the browser silences its engine —
  // which is why the position is saved per sentence (localStorage) and offered
  // again, paused right there, when the thread is next opened.

  _toggleAutoplay() {
    this._autoplay = !this._autoplay;
    try { localStorage.setItem('retinue-voice-autoplay', this._autoplay ? '1' : '0'); } catch (_e) { /* ignore */ }
    this.render();
  }

  // The message's own button: play it, or pause/resume it when it is the one
  // in the player (a resume goes on from where it was, never from the top).
  _onSpeakButton(btn) {
    const idx = Number(btn.dataset.speakIdx);
    const t = this._thread;
    const m = (t && t.messages) ? t.messages[idx] : null;
    if (!m) return;
    if (this._isLoaded(t, m)) { this._reader.toggle(); return; }
    this._play(t, m, 0);
  }

  // Load message `m` of thread `t` into the player and speak it from `fraction`
  // (0..1) of its text. Called from a tap where possible: the first speak of a
  // page load must sit inside a user gesture on iOS.
  _play(t, m, fraction) {
    if (!t || !m || !speechAvailable()) return;
    const clean = this._plainForSpeech(m.text);
    if (!clean) return;
    this._playing = this._playingInfo(t, m);
    this._reader.load([{ id: m.ts, lang: m.lang, text: clean }], { fraction: fraction || 0 });
    this._reader.resume();
  }

  _playingInfo(t, m) {
    const who = m.agent || (m.role === 'agent' ? 'Retinue' : 'Ara');
    return { conv: t.id, ts: m.ts, who, title: t.title || '' };
  }

  // Is `m` (of thread `t`) the message currently in the player?
  _isLoaded(t, m) {
    const p = this._playing;
    return !!(p && t && m && this._reader.loaded && p.conv === t.id && p.ts === m.ts);
  }

  _speakBtnHtml(t, m, idx) {
    const loaded = this._isLoaded(t, m);
    const playing = loaded && this._reader.speaking;
    const label = playing ? 'Pause' : (loaded ? 'Resume reading' : 'Read aloud');
    const glyph = playing ? '⏸' : (loaded ? '▶' : '\u{1F50A}');
    return `<button class="speak${loaded ? ' on' : ''}" type="button" data-speak-idx="${idx}" ` +
      `title="${label}" aria-label="${label}">${glyph}</button>`;
  }

  // A reading interrupted by leaving the page is offered again when its thread
  // opens: the message goes into the player, paused, at the sentence it was in.
  // Only when the player is free — a reading in progress is never displaced.
  _restorePosition(t) {
    if (!t || this._reader.loaded || !speechAvailable()) return;
    const saved = this._savedPosition();
    if (!saved || saved.conv !== t.id) return;
    const m = (t.messages || []).find((x) => x.role !== 'user' && x.ts === saved.ts);
    if (!m) return;
    const clean = this._plainForSpeech(m.text);
    if (!clean) return;
    this._playing = this._playingInfo(t, m);
    this._reader.load([{ id: m.ts, lang: m.lang, text: clean }], { fraction: saved.fraction });
  }

  _savedPosition() {
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
  _savePosition(ev) {
    const p = this._playing;
    if (!p) return;
    try {
      if (!(ev.fraction > 0 && ev.fraction < 1)) { localStorage.removeItem(POSITION_KEY); return; }
      localStorage.setItem(POSITION_KEY, JSON.stringify({
        conv: p.conv, ts: p.ts, fraction: ev.fraction, at: Date.now(),
      }));
    } catch (_e) { /* ignore */ }
  }

  _clearPosition() {
    try { localStorage.removeItem(POSITION_KEY); } catch (_e) { /* ignore */ }
  }

  // The ✕ on the bar: stop for good, and forget the place.
  _closePlayer() {
    this._reader.stop();
    this._clearPosition();
  }

  // Every reader transition lands here (and a tick every quarter second while
  // it plays). Bookkeeping first, then the DOM — in place, never a re-render:
  // a full render mid-reading would drop the composer's focus and the scroll.
  _onReaderProgress(ev) {
    if (ev.event === 'end') this._clearPosition();
    else if (ev.event === 'load' || ev.event === 'start' || ev.event === 'piece' ||
             ev.event === 'pause' || ev.event === 'seek') this._savePosition(ev);
    if (ev.event === 'stop') this._playing = null;
    if (ev.event === 'tick') { this._updatePlayer(); return; }
    this._applyPlayerState();
  }

  _playerHostHtml() {
    return `<div class="player-host" data-key="${esc(this._playerKey())}">${this._playerHtml()}</div>`;
  }

  // What the bar's structure depends on; when it changes the bar is rebuilt,
  // otherwise only its slider and label move (see _updatePlayer).
  _playerKey() {
    const r = this._reader;
    const p = this._playing;
    if (!r.loaded || !p) return 'idle';
    return [r.state, p.conv, p.ts, r.error || '', p.conv === this._active ? 'here' : 'away'].join('|');
  }

  _playerHtml() {
    const r = this._reader;
    const p = this._playing;
    if (!r.loaded || !p) return '';
    const playing = r.speaking;
    // Read from another view (the list, or a different thread): name the
    // thread too, and make it a way back to the message.
    const elsewhere = p.conv !== this._active;
    const who = esc(p.who) + (elsewhere && p.title ? ` · ${esc(p.title)}` : '');
    const whoEl = elsewhere
      ? `<button type="button" class="p-who p-link" data-p-open="${esc(p.conv)}" ` +
        `title="Open this conversation">${who}</button>`
      : `<span class="p-who">${who}</span>`;
    const main = playing ? 'Pause' : 'Play';
    return `<div class="player" role="group" aria-label="Read aloud">` +
      `<button type="button" class="p-btn" data-p-back title="Back one sentence" ` +
      `aria-label="Back one sentence">⏮</button>` +
      `<button type="button" class="p-btn p-main" data-p-toggle title="${main}" aria-label="${main}">` +
      `${playing ? '⏸' : '▶'}</button>` +
      `<button type="button" class="p-btn" data-p-fwd title="Forward one sentence" ` +
      `aria-label="Forward one sentence">⏭</button>` +
      `<div class="p-track">` +
      `<input type="range" class="p-seek" min="0" max="1000" step="1" value="0" ` +
      `aria-label="Position in the message" data-p-seek>` +
      `<div class="p-info">${whoEl}<span class="p-pos" data-p-pos></span></div></div>` +
      `<button type="button" class="p-btn p-close" data-p-close title="Stop reading" ` +
      `aria-label="Stop reading">✕</button></div>`;
  }

  // Listeners live on the host, which every full render creates anew and the
  // in-place rebuilds below keep — so they are attached once per render.
  _wirePlayer() {
    const host = this.shadowRoot && this.shadowRoot.querySelector('.player-host');
    if (!host) return;
    if (!host.dataset.wired) {
      host.dataset.wired = '1';
      host.addEventListener('click', (e) => {
        if (e.target.closest('[data-p-toggle]')) { this._reader.toggle(); return; }
        if (e.target.closest('[data-p-back]')) { this._reader.back(); return; }
        if (e.target.closest('[data-p-fwd]')) { this._reader.forward(); return; }
        if (e.target.closest('[data-p-close]')) { this._closePlayer(); return; }
        const open = e.target.closest('[data-p-open]');
        if (open) this._openThread(open.getAttribute('data-p-open'));
      });
      // Dragging previews the position; releasing seeks there.
      host.addEventListener('input', (e) => {
        if (!e.target.matches('[data-p-seek]')) return;
        this._scrubbing = true;
        this._updatePlayer(Number(e.target.value) / 1000);
      });
      host.addEventListener('change', (e) => {
        if (!e.target.matches('[data-p-seek]')) return;
        this._scrubbing = false;
        this._reader.seek(Number(e.target.value) / 1000);
      });
    }
    this._updatePlayer();
  }

  // Bring the bar and the message buttons in line with the reader, in place.
  _applyPlayerState() {
    const root = this.shadowRoot;
    if (!root) return;
    const host = root.querySelector('.player-host');
    if (host) {
      const key = this._playerKey();
      if (host.dataset.key !== key) {
        host.dataset.key = key;
        host.innerHTML = this._playerHtml();
      }
      this._wirePlayer();
    }
    const t = this._thread;
    if (!t || !this._active) return;
    root.querySelectorAll('.speak[data-speak-idx]').forEach((btn) => {
      const m = (t.messages || [])[Number(btn.dataset.speakIdx)];
      if (!m) return;
      const loaded = this._isLoaded(t, m);
      const playing = loaded && this._reader.speaking;
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

  // Slider and label only. `preview` (0..1) is the value under the user's
  // finger while dragging; otherwise the reader's own position is shown.
  _updatePlayer(preview) {
    const root = this.shadowRoot;
    const bar = root && root.querySelector('.player');
    if (!bar) return;
    const pr = this._reader.progress();
    const previewing = typeof preview === 'number';
    const fraction = previewing ? preview : pr.fraction;
    const seek = bar.querySelector('[data-p-seek]');
    if (seek) {
      if (!this._scrubbing || previewing) seek.value = String(Math.round(fraction * 1000));
      seek.style.setProperty('--p', `${(fraction * 100).toFixed(1)}%`);
      seek.setAttribute('aria-valuetext', `${Math.round(fraction * 100)}%`);
    }
    const pos = bar.querySelector('[data-p-pos]');
    if (pos) {
      const text = this._positionLabel(pr, fraction, previewing);
      if (pos.textContent !== text) pos.textContent = text;
    }
  }

  _positionLabel(pr, fraction, previewing) {
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

  // Strip Markdown so the synthesizer reads clean prose: no code fences,
  // backticks, emphasis marks, quote markers, list bullets or table rules;
  // links and chips read as their labels, a raw URL as its host.
  _plainForSpeech(text) {
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

  // When autoplay is on, speak assistant messages that arrive after the thread
  // was opened. The first look at a thread only records its existing messages as
  // "seen" so historical replies are never blurted out on open.
  _maybeAutoplay(t) {
    if (!t || !speechAvailable()) return;
    const cid = t.id;
    if (!this._spoken[cid]) this._spoken[cid] = new Set();
    const seen = this._spoken[cid];
    const replies = (t.messages || []).filter((m) => m.role !== 'user' && (m.text || '').trim());
    if (!this._autoReady[cid]) {
      replies.forEach((m) => seen.add(m.ts));
      this._autoReady[cid] = true;
      return;
    }
    const fresh = replies.filter((m) => !seen.has(m.ts));
    fresh.forEach((m) => seen.add(m.ts));
    if (!this._autoplay || !fresh.length) return;
    const last = fresh[fresh.length - 1];
    this._play(t, last, 0);
  }

  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll('[data-open]').forEach((el) =>
      el.addEventListener('click', () => this._openThread(el.getAttribute('data-open'))));
    const nw = root.querySelector('[data-new]');
    if (nw) nw.addEventListener('click', () => this._openComposer());
    const back = root.querySelector('[data-back]');
    if (back) back.addEventListener('click', () => this._openList());
    const arch = root.querySelector('[data-archive]');
    if (arch) arch.addEventListener('click', () => this._archive(this._active, true));
    const unarch = root.querySelector('[data-unarchive]');
    if (unarch) unarch.addEventListener('click', () => this._archive(this._active, false));
    root.querySelectorAll('[data-scope]').forEach((el) =>
      el.addEventListener('click', () => this._setScope(el.getAttribute('data-scope'))));
    root.querySelectorAll('[data-setview]').forEach((el) =>
      el.addEventListener('click', () => {
        this._view = el.getAttribute('data-setview');
        setViewPref('conversations', this._view);
        this.render();
      }));
    // Delegate copy-button clicks on the thread container: it survives the
    // in-place innerHTML swaps of _partialUpdate, so one listener covers the
    // quote-block copy buttons across polls.
    const threadEl = root.querySelector('.thread');
    if (threadEl) {
      threadEl.addEventListener('click', (e) => {
        const btn = e.target.closest('.copy');
        if (btn) { this._copyToClipboard(btn); return; }
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
    this._wirePlayer();
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
      // Persist what the user is typing so a background poll re-render doesn't
      // wipe it (the input's value is rebuilt from this._drafts on each render).
      input.addEventListener('input', () => {
        const draftKey = this._active || (this._composing ? 'composer' : '');
        if (draftKey) {
            this._drafts[draftKey] = input.value;
        }
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
        const draftKey = this._active || (this._composing ? 'composer' : '');
        const currentOutFiles = draftKey ? (this._outFiles[draftKey] || []) : [];
        if (text.trim() || currentOutFiles.length) this._send(text);
      });
      // Restore focus and caret after a re-render so typing isn't interrupted,
      // but only when the field already had focus or a view was just opened —
      // a background poll re-render must not steal focus or pop the keyboard.
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

const CSS = `
  :host { display: flex; flex-direction: column; min-height: 0; height: 100%; }
  * { box-sizing: border-box; }
  button { font: inherit; }
  button:focus-visible, a:focus-visible, textarea:focus-visible {
    outline: 2px solid var(--accent, #6ea8fe); outline-offset: 1px; }

  /* The card is chrome-less on phones (edge-to-edge, app-like) and becomes a
     framed card again on wide screens where the page has room around it. */
  .card { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  @media (min-width: 700px) {
    .card { background: var(--card, #151922); border: 1px solid var(--line, rgba(231, 235, 242, .08));
            border-radius: var(--radius, 16px); padding: 14px 16px; }
  }
  header { flex: none; display: flex; align-items: center; justify-content: space-between;
           gap: 8px; padding: 0 2px 10px; }
  h2 { font-size: .82rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
       color: var(--muted, #8b93a3); margin: 0; }
  .badge { background: var(--high, #ff6b6b); color: #fff; font-size: .7rem; font-weight: 700;
           border-radius: 10px; padding: 1px 7px; }
  .content { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .muted { color: var(--muted, #8b93a3); margin: 4px 0; }

  /* ── List view ─────────────────────────────────────────────────────────── */
  /* On phones the PAGE is the scroller (see styles.css), so .tabs must NOT be
     a scroll container there: a touch that starts on a row would latch onto it
     (any subpixel overflow makes it "scrollable") and overscroll-behavior:
     contain would then swallow the gesture instead of chaining it to the page
     — leaving only the thin margin outside the card scrollable by finger.
     Only the wide layout, where the frame is fixed and the list genuinely
     scrolls internally, makes it a (contained) scroller.

     Threads are tiles that reflow into as many columns as fit: one on a phone
     (min(100%, …) collapses the track to whatever width there is), several once
     the card is wide — where the list also shows every thread, not five (see
     _shownThreads). So the room is used in both directions instead of five rows
     being stretched across a desktop column. align-content: start keeps a short
     list at its natural height rather than blowing the tiles up. */
  .tabs { flex: 1; min-height: 0; display: grid; align-content: start;
          grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr));
          gap: 8px; padding: 2px; }
  /* The header's view toggle (base.js) forces a single full-width column. */
  .tabs.as-list { grid-template-columns: minmax(0, 1fr); }
  .empty { grid-column: 1 / -1; }
  @media (min-width: 1000px) and (min-height: 480px) {
    .tabs { overflow-y: auto; overscroll-behavior: contain; }
  }
  .tab { flex: none; text-align: left; background: var(--card-2, #1c2230); border: 0;
         border-radius: 14px; padding: 11px 13px; color: var(--fg, #e7ebf2); cursor: pointer;
         display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: baseline;
         gap: 2px 10px; -webkit-tap-highlight-color: transparent;
         user-select: none; -webkit-user-select: none; touch-action: manipulation; }
  /* Hover affordance only where a hover pointer exists — on touch screens the
     sticky :hover outline reads as the row being "selected" by a scroll touch. */
  @media (hover: hover) {
    .tab:hover { outline: 1px solid var(--accent, #6ea8fe); }
  }
  .tab.unread { box-shadow: inset 3px 0 0 0 var(--accent, #6ea8fe); }
  .t-title { display: flex; align-items: center; gap: 7px; min-width: 0; font-weight: 600; }
  /* Titles are what the list is read for, so give them a second line before
     cutting: agent-opened threads carry a whole subject line, and one line of
     ellipsis hid most of it. */
  .t-name { overflow: hidden; overflow-wrap: anywhere; display: -webkit-box;
            -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--high, #ff6b6b); }
  .tag-edit { flex: none; font-size: .62rem; font-weight: 700; letter-spacing: .04em;
              text-transform: uppercase; color: var(--accent, #6ea8fe);
              border: 1px solid var(--accent, #6ea8fe); border-radius: 6px; padding: 1px 5px; }
  .t-meta { color: var(--muted, #8b93a3); font-size: .72rem; white-space: nowrap; }
  .t-prev { grid-column: 1 / -1; color: var(--muted, #8b93a3); font-size: .8rem; line-height: 1.35;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .about-chip { flex: none; align-self: flex-start; margin-top: 10px; padding: 5px 12px;
                border-radius: 999px; background: var(--card-2, #1c2230);
                border: 1px solid var(--accent, #6ea8fe); color: var(--fg, #e7ebf2);
                font-size: .78rem; font-weight: 600; max-width: 100%;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
           gap: 6px; color: var(--muted, #8b93a3); text-align: center; padding: 24px 12px; }
  .empty .e-ico { font-size: 2rem; opacity: .55; }
  .empty p { margin: 0; max-width: 32ch; }
  .list-foot { flex: none; display: flex; flex-direction: column; gap: 10px; padding-top: 12px; }
  .new { width: 100%; background: var(--accent, #6ea8fe); color: #0b0d12; border: 0;
         border-radius: 14px; padding: 12px; font-weight: 650; font-size: .95rem; cursor: pointer;
         -webkit-tap-highlight-color: transparent; }
  .new:active { filter: brightness(1.12); }
  .filter { flex: none; display: flex; background: var(--card-2, #1c2230); border-radius: 12px;
            padding: 3px; margin-bottom: 10px; }
  .filter-tab { flex: 1; background: transparent; border: 0; border-radius: 9px; padding: 7px;
                color: var(--muted, #8b93a3); cursor: pointer; }
  .filter-tab.on { background: var(--accent, #6ea8fe); color: #0b0d12; font-weight: 600; }
  .all-link { color: var(--accent, #6ea8fe); text-decoration: none; font-size: .85rem;
              text-align: center; padding: 2px; }
  .all-link:hover { text-decoration: underline; }

  /* ── Thread view ───────────────────────────────────────────────────────── */
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
  .thread { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
            display: flex; flex-direction: column; gap: 12px; padding: 12px 2px; }
  /* An open thread takes the whole frame, which on a wide display is far wider
     than a comfortable line. Centre the messages and the composer in a reading
     column; the bar keeps its full-width divider. */
  @media (min-width: 1000px) {
    .thread, .composer, .player-host {
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

  /* ── Read-aloud player ─────────────────────────────────────────────────── */
  /* Present (empty) in every view, so the bar can appear without a re-render
     and follow the reading into the list. Sits above the composer. */
  .player-host { flex: none; }
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

customElements.define('retinue-conversations', RetinueConversations);
