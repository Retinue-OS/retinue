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

import { esc, fmtAge } from './base.js';
import { renderMarkdown, MD_CSS } from './markdown.js';

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
// Most recent threads shown on the compact dashboard card before the user is
// sent to the dedicated all-conversations page for the rest.
const MAX_CARD_THREADS = 5;
const POLL_MS = 4000;
const PENDING_WARN_SECONDS = 2 * 60;
const PENDING_STALE_SECONDS = 10 * 60;
const TEXTAREA_MAX_HEIGHT_RATIO = 0.35;
// Keep the client cap in step with the gateway's CONVERSATION_MAX_ATTACHMENT_BYTES
// (default 25 MiB) so oversized files are rejected before a doomed upload.
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
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
    this._scope = 'active';  // full-mode thread filter: active|archived|edits
    this._composeProject = null;      // project URI the composer is about, if any
    this._composeProjectTitle = '';   // its display title (for the chip)
    this._pushDepth = 0;     // history entries we pushed and have not unwound
    // Voice: record a message (server transcribes) and speak replies back.
    this._recState = 'idle'; // idle | recording | transcribing
    this._recChunks = [];
    this._mediaRecorder = null;
    this._recStream = null;
    this._autoplay = false;  // speak Ara's replies as they arrive
    try { this._autoplay = localStorage.getItem('retinue-voice-autoplay') === '1'; } catch (_e) { /* ignore */ }
    this._autosend = false;  // send a dictation straight off, without the review step
    try { this._autosend = localStorage.getItem('retinue-voice-autosend') === '1'; } catch (_e) { /* ignore */ }
    this._spoken = {};       // per-thread set of message ts already voiced/seen
    this._autoReady = {};    // per-thread: initial history marked, future msgs autoplay
    this._speakingTs = null; // ts of the message currently being spoken, if any
    // getVoices() is empty until the engine loads its list; warm it up so a
    // German voice is available by the time the user taps play.
    try {
      if ('speechSynthesis' in window) {
        window.speechSynthesis.getVoices();
        window.speechSynthesis.addEventListener('voiceschanged', () => {
          try { window.speechSynthesis.getVoices(); } catch (_e) { /* ignore */ }
        });
      }
    } catch (_e) { /* ignore */ }
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._full = this.hasAttribute('full');
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
    this.render();
    this.refresh();
    this._timer = setInterval(() => this.refresh(), POLL_MS);
  }

  disconnectedCallback() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
    if (this._onPop) {
      window.removeEventListener('popstate', this._onPop);
      window.removeEventListener('hashchange', this._onPop);
    }
    this._onPop = null;
    this._stopRecording();
    this._stopStream();
    try { if ('speechSynthesis' in window) window.speechSynthesis.cancel(); } catch (_e) { /* ignore */ }
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

  // In full mode the filter can request the archived scope or the (normally
  // hidden) project edit-command threads; otherwise we list active chat
  // threads — the default the dashboard card and agents expect.
  _listUrl() {
    if (this._full && this._scope === 'archived') return `${LIST_URL}?archived=1`;
    if (this._full && this._scope === 'edits') return `${LIST_URL}?all=1&kind=edit`;
    return LIST_URL;
  }

  // The card shows only the most recent threads; the full page shows them all.
  _shownThreads() {
    return this._full ? this._threads : this._threads.slice(0, MAX_CARD_THREADS);
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
        hdr.insertAdjacentHTML('beforeend', `<span class="badge">${n}</span>`);
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

  // Switch the full-page Active/Archived filter and reload that scope.
  _setScope(scope) {
    if (this._scope === scope) return;
    this._scope = scope;
    this._threads = [];
    this.render();
    this.refresh();
  }

  async _send(text) {
    const draftKey = this._active || (this._composing ? 'composer' : '');
    const currentOutFiles = this._outFiles[draftKey] || [];
    // A message needs text or at least one attachment.
    if (this._busy || (!text.trim() && !currentOutFiles.length)) return;
    this._busy = true;
    try {
      const body = { message: text };
      // A composer opened from a project page links the new thread to that
      // project, so Ara starts from the project file's current state.
      if (this._composing && this._composeProject) {
        body.project = this._composeProject;
        if (this._composeProjectTitle) body.project_title = this._composeProjectTitle;
      }
      if (currentOutFiles.length) {
        body.attachments = currentOutFiles.map((f) => ({
          filename: f.name, content_type: f.type, data: f.data,
        }));
      }
      const url = this._active ? `/conversations/${this._active}/messages` : LIST_URL;
      const res = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(String(res.status));
      const conv = await res.json();
      // A thread opened from the composer reuses the composer's history entry,
      // so back still lands on the list rather than the (now gone) composer.
      if (this._composing) history.replaceState(null, '', `#conversation-${conv.id}`);
      this._active = conv.id;
      this._thread = conv;
      this._composing = false;
      this._setComposeProject(undefined); // the link was consumed by this thread
      this._drafts[conv.id] = '';
      this._drafts['composer'] = '';
      this._outFiles[conv.id] = [];
      this._outFiles['composer'] = [];
      this._attachError = '';
    } catch (_err) {
      // surface a soft failure inline by leaving the input; re-render shows state
    } finally {
      this._busy = false;
      this.render();
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
        `${this._unreadCount() ? `<span class="badge">${this._unreadCount()}</span>` : ''}</header>`
      : '';
    this.shadowRoot.innerHTML = `<style>${CSS}${MD_CSS}</style>` +
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
      (t.messages || []).map((m) => [m.role, m.text, m.ts, (m.attachments || []).length]),
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
      `<div class="tabs">${this._tabsHtml()}${this._emptyHtml()}</div>` +
      `<div class="list-foot">${newBtn}${this._footerHtml()}</div>`;
  }

  // Active/Archived/Edits switch — only in the dedicated full-page view. The
  // Edits filter is where the normally hidden project edit-command threads
  // remain reachable.
  _filterHtml() {
    if (!this._full) return '';
    const tab = (scope, label) =>
      `<button class="filter-tab${this._scope === scope ? ' on' : ''}" data-scope="${scope}">${label}</button>`;
    return `<div class="filter">${tab('active', 'Active')}${tab('archived', 'Archived')}${tab('edits', 'Edits')}</div>`;
  }

  _emptyHtml() {
    if (this._threads.length) return '';
    const msg = (this._full && this._scope === 'archived')
      ? 'No archived conversations.'
      : (this._full && this._scope === 'edits')
        ? 'No edit commands yet. Dictate or type one on a project page.'
        : 'No conversations yet.';
    return `<div class="empty"><span class="e-ico" aria-hidden="true">&#x1F4AC;</span><p>${msg}</p></div>`;
  }

  // The card links out to the full page; the full page links back home.
  _footerHtml() {
    if (this._full) return '<a class="all-link" href="/">&larr; Back to dashboard</a>';
    return `<a class="all-link" href="/conversations.html">${this._allLinkLabel()}</a>`;
  }

  _allLinkLabel() {
    const more = this._threads.length > MAX_CARD_THREADS
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

  // Dictating and then still having to press Send is a step too many for a
  // user who dictates everything — this toggle skips the review and sends. It
  // is a voice *setting*, not a per-message action, so it sits in the top bar
  // next to the speak toggle rather than eating width in the input row.
  _autosendBtnHtml() {
    const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
    if (!canRecord) return '';
    const title = this._autosend
      ? 'Dictation is sent right away — tap to review before sending'
      : 'Dictation goes to the input field for review — tap to send it right away';
    return `<button type="button" class="iconbtn${this._autosend ? ' on' : ''}" data-autosend ` +
      `title="${title}" aria-label="${title}" aria-pressed="${this._autosend}">⚡</button>`;
  }

  _composerView() {
    // Coming from a project page, show what the new thread will be about.
    const projectChip = this._composeProject
      ? `<div class="about-chip">About: ${esc(this._composeProjectTitle || this._composeProject)}</div>`
      : '';
    const hint = this._composeProject
      ? `<p>Ask Ara about this project &mdash; she reads its current state first.</p>`
      : `<p>Ask Ara anything &mdash; she picks it up with full context.</p>`;
    return `<div class="thread-bar">${this._backBtnHtml()}` +
      `<span class="bar-title">New conversation</span>` +
      `<span class="bar-actions">${this._autosendBtnHtml()}</span></div>` +
      projectChip +
      `<div class="empty"><span class="e-ico" aria-hidden="true">&#x1F4AC;</span>` +
      hint + `</div>` +
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
    const autoBtn = ('speechSynthesis' in window)
      ? `<button class="iconbtn${this._autoplay ? ' on' : ''}" data-autoplay ` +
        `title="Speak Ara's replies as they arrive" aria-label="Speak replies as they arrive" ` +
        `aria-pressed="${this._autoplay}">${this._autoplay ? '\u{1F50A}' : '\u{1F507}'}</button>`
      : '';
    return `<div class="thread-bar">${this._backBtnHtml()}` +
      `<span class="bar-title" data-title>${esc(t.title || 'Conversation')}</span>` +
      `<span class="bar-actions">${this._autosendBtnHtml()}${autoBtn}${archiveBtn}</span></div>` +
      `<div class="thread">${this._messagesHtml(t)}</div>` +
      this._inputRow('Reply …');
  }

  _messagesHtml(t) {
    const canSpeak = 'speechSynthesis' in window;
    const msgs = (t.messages || []).map((m, idx) => {
      const cls = m.role === 'user' ? 'me' : (m.role === 'agent' ? 'agent' : 'ara');
      const who = m.role === 'user' ? 'You' : (m.role === 'agent' ? 'Retinue' : 'Ara');
      const speakBtn = (canSpeak && m.role !== 'user' && (m.text || '').trim())
        ? `<button class="speak" type="button" data-speak-idx="${idx}" ` +
          `title="Play message" aria-label="Play message">\u{1F50A}</button>`
        : '';
      return `<div class="msg ${cls}"><div class="msg-head"><small class="who">${esc(who)}</small>` +
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
    const errRow = this._attachError ? `<div class="attach-err">${esc(this._attachError)}</div>` : '';
    const currentDraft = draftKey ? (this._drafts[draftKey] || '') : '';
    const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
    const micLabel = this._recState === 'recording' ? '\u23F9'
      : (this._recState === 'transcribing' ? '\u2026' : '\u{1F3A4}');
    const micTitle = this._recState === 'recording' ? 'Stop recording'
      : (this._recState === 'transcribing' ? 'Transcribing …' : 'Record a voice message');
    const micDisabled = (this._busy || this._recState === 'transcribing') ? 'disabled' : '';
    const micBtn = canRecord
      ? `<button type="button" class="mic${this._recState === 'recording' ? ' rec' : ''}" ` +
        `data-mic title="${micTitle}" aria-label="${micTitle}" ${micDisabled}>${micLabel}</button>`
      : '';
    // A lean row keeps the width for the text field: mic on the left, the
    // attach control tucked inside the field, send on the right. The auto-send
    // toggle lives in the top bar (see _autosendBtnHtml).
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

  // ── Voice input: record → server transcribe → drop into the composer ───────
  // The server repairs the transcript before returning it, so what lands in the
  // draft is readable rather than raw Whisper output. It still lands in the
  // draft, to be read and corrected before Send — unless auto-send is on, in
  // which case the dictation goes straight to Ara, as it does over Signal.
  async _toggleRecord() {
    if (this._recState === 'transcribing') return;
    if (this._recState === 'recording') { this._stopRecording(); return; }
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder)) {
      this._attachError = 'Voice recording is not supported on this device.';
      this.render();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this._recStream = stream;
      this._recChunks = [];
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
    } catch (_err) {
      this._recState = 'idle';
      this._attachError = 'Microphone access was denied.';
      this._stopStream();
      this.render();
    }
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
    this._stopStream();
    const chunks = this._recChunks || [];
    this._recChunks = [];
    const type = (this._mediaRecorder && this._mediaRecorder.mimeType)
      || (chunks[0] && chunks[0].type) || 'audio/webm';
    this._mediaRecorder = null;
    if (!chunks.length) { this._recState = 'idle'; this.render(); return; }
    const blob = new Blob(chunks, { type });
    this._recState = 'transcribing';
    this.render();
    let toSend = '';
    try {
      // The open thread is context for the cleanup pass: it is what tells the
      // model which names and topics this dictation is likely to be about.
      const q = this._active ? `?thread=${encodeURIComponent(this._active)}` : '';
      const res = await fetch(`/conversations/transcribe${q}`, {
        method: 'POST',
        headers: { 'Content-Type': blob.type || 'application/octet-stream' },
        body: blob,
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const text = ((data && data.text) || '').trim();
      if (text) {
        this._appendToDraft(text);
        // Send the whole draft, so anything typed before dictating comes along.
        const draftKey = this._active || (this._composing ? 'composer' : '');
        if (this._autosend && draftKey) toSend = this._drafts[draftKey] || '';
      } else {
        this._attachError = 'No speech was detected in the recording.';
      }
    } catch (_err) {
      this._attachError = "Couldn't transcribe the recording. Please try again.";
    } finally {
      this._recState = 'idle';
      this._focusNext = true;
      this.render();
    }
    // _send() re-renders; on failure it leaves the draft in place to retry.
    if (toSend) await this._send(toSend);
  }

  _toggleAutosend() {
    this._autosend = !this._autosend;
    try { localStorage.setItem('retinue-voice-autosend', this._autosend ? '1' : '0'); } catch (_e) { /* ignore */ }
    this.render();
  }

  _appendToDraft(text) {
    const draftKey = this._active || (this._composing ? 'composer' : '');
    if (!draftKey) return;
    const cur = this._drafts[draftKey] || '';
    this._drafts[draftKey] = cur ? `${cur.replace(/\s*$/, '')} ${text}` : text;
  }

  // ── Voice output: speak Ara's replies via the browser's speech synth ───────
  _toggleAutoplay() {
    this._autoplay = !this._autoplay;
    try { localStorage.setItem('retinue-voice-autoplay', this._autoplay ? '1' : '0'); } catch (_e) { /* ignore */ }
    if (!this._autoplay) {
      try { window.speechSynthesis.cancel(); } catch (_e) { /* ignore */ }
      this._speakingTs = null;
    }
    this.render();
  }

  _onSpeakButton(btn) {
    const idx = Number(btn.dataset.speakIdx);
    const msgs = (this._thread && this._thread.messages) || [];
    const m = msgs[idx];
    if (!m) return;
    // Second tap on the message being spoken stops it.
    if (this._speakingTs === m.ts && window.speechSynthesis && window.speechSynthesis.speaking) {
      try { window.speechSynthesis.cancel(); } catch (_e) { /* ignore */ }
      this._speakingTs = null;
      return;
    }
    this._speak(m.text, m.lang, m.ts);
  }

  _speak(text, lang, ts) {
    if (!('speechSynthesis' in window)) return;
    const clean = this._plainForSpeech(text);
    if (!clean) return;
    try {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(clean);
      // Prefer the server-provided tag; fall back to detecting from the text so
      // messages stored before the server attached `lang` still read correctly.
      const code = lang || this._detectLang(clean);
      if (code) {
        u.lang = code;
        // Setting u.lang alone is not enough in some browsers — they keep the
        // default (often English) voice. Pick a matching voice explicitly.
        const voice = this._voiceFor(code);
        if (voice) u.voice = voice;
      }
      this._speakingTs = ts || null;
      const done = () => { if (this._speakingTs === (ts || null)) this._speakingTs = null; };
      u.addEventListener('end', done);
      u.addEventListener('error', done);
      window.speechSynthesis.speak(u);
    } catch (_e) { /* ignore */ }
  }

  // Cheap German-vs-English guess for speech synthesis when the message carries
  // no server-provided language tag. Returns a BCP-47 code or null.
  _detectLang(text) {
    const s = String(text || '').toLowerCase();
    if (!s.trim()) return null;
    if (/[äöüß]/.test(s)) return 'de';
    const words = s.match(/[a-zäöüß]+/g) || [];
    if (words.length < 3) return null;
    const de = new Set(['und', 'oder', 'aber', 'nicht', 'ist', 'sind', 'der',
      'die', 'das', 'den', 'dem', 'ein', 'eine', 'ich', 'du', 'sie', 'wir',
      'mit', 'auf', 'für', 'auch', 'noch', 'schon', 'kein', 'wenn', 'dann',
      'weil', 'dass', 'sich', 'hier', 'dein', 'deine', 'habe', 'hat', 'haben',
      'kann', 'soll', 'muss', 'wie', 'was', 'wo', 'über', 'bitte', 'danke',
      'gemacht', 'geht', 'mehr', 'gibt']);
    const hits = words.reduce((n, w) => n + (de.has(w) ? 1 : 0), 0);
    return (hits >= 2 || hits / words.length >= 0.12) ? 'de' : 'en';
  }

  // Pick a speechSynthesis voice whose language matches `code` (e.g. 'de').
  // Prefers a local voice; caches nothing since getVoices() may populate late.
  _voiceFor(code) {
    const want = String(code || '').slice(0, 2).toLowerCase();
    if (!want) return null;
    let voices = [];
    try { voices = window.speechSynthesis.getVoices() || []; } catch (_e) { return null; }
    const match = voices.filter((v) => (v.lang || '').slice(0, 2).toLowerCase() === want);
    if (!match.length) return null;
    return match.find((v) => v.localService) || match[0];
  }

  // Strip Markdown so the synthesizer reads clean prose (no backticks, asterisks,
  // "greater-than" quote markers, or raw URLs — link labels are kept).
  _plainForSpeech(text) {
    return String(text == null ? '' : text)
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/^\s*>\s?/gm, '')
      .replace(/\[([^\]]+)\]\((?:[^)]+)\)/g, '$1')
      .replace(/[*_#]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  // When autoplay is on, speak assistant messages that arrive after the thread
  // was opened. The first look at a thread only records its existing messages as
  // "seen" so historical replies are never blurted out on open.
  _maybeAutoplay(t) {
    if (!t || !('speechSynthesis' in window)) return;
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
    this._speak(last.text, last.lang, last.ts);
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
    // Delegate copy-button clicks on the thread container: it survives the
    // in-place innerHTML swaps of _partialUpdate, so one listener covers the
    // quote-block copy buttons across polls.
    const threadEl = root.querySelector('.thread');
    if (threadEl) {
      threadEl.addEventListener('click', (e) => {
        const btn = e.target.closest('.copy');
        if (btn) { this._copyToClipboard(btn); return; }
        const sbtn = e.target.closest('.speak');
        if (sbtn) this._onSpeakButton(sbtn);
      });
    }
    const mic = root.querySelector('[data-mic]');
    if (mic) mic.addEventListener('click', () => this._toggleRecord());
    const autosend = root.querySelector('[data-autosend]');
    if (autosend) autosend.addEventListener('click', () => this._toggleAutosend());
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
  .tabs { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
          display: flex; flex-direction: column; gap: 8px; padding: 2px; }
  .tab { flex: none; text-align: left; background: var(--card-2, #1c2230); border: 0;
         border-radius: 14px; padding: 11px 13px; color: var(--fg, #e7ebf2); cursor: pointer;
         display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: baseline;
         gap: 2px 10px; -webkit-tap-highlight-color: transparent; }
  .tab:hover { outline: 1px solid var(--accent, #6ea8fe); }
  .tab.unread { box-shadow: inset 3px 0 0 0 var(--accent, #6ea8fe); }
  .t-title { display: flex; align-items: center; gap: 7px; min-width: 0; font-weight: 600; }
  .t-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
  .thread { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
            display: flex; flex-direction: column; gap: 12px; padding: 12px 2px; }
  .msg { display: flex; flex-direction: column; gap: 3px; max-width: 86%; }
  .msg.me { align-self: flex-end; align-items: flex-end; }
  .who { color: var(--muted, #8b93a3); font-size: .7rem; }
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
  .mic { display: inline-flex; align-items: center; justify-content: center; height: 40px; width: 40px;
         flex: none; border-radius: 50%; background: var(--card-2, #1c2230); border: 0; cursor: pointer;
         color: var(--fg, #e7ebf2); font-size: 1.05rem; user-select: none;
         -webkit-tap-highlight-color: transparent; }
  .mic:hover { background: rgba(110, 168, 254, .2); }
  .mic.rec { background: var(--high, #ff6b6b); color: #fff; animation: mic-pulse 1.2s ease-in-out infinite; }
  .mic[disabled] { opacity: .6; cursor: default; }
  @keyframes mic-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .55; } }
  .msg-head { display: flex; align-items: center; gap: 6px; }
  .msg.me .msg-head { flex-direction: row-reverse; }
  .speak { background: transparent; border: 0; cursor: pointer; padding: 0 2px; font-size: .8rem;
           line-height: 1; opacity: .65; }
  .speak:hover { opacity: 1; }
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
