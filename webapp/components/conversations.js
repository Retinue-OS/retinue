// Conversation tabs: the list of threads with Ara, and the one that is open.
//
// Unlike the other cards (which render one static JSON document), this card is
// interactive and talks to the gateway's conversation API:
//   GET  /conversations                 list of active threads (tabs)
//   GET  /conversations?archived=1      list of archived threads
//   GET  /conversations?all=1&kind=…    the normally hidden kinds (full page)
//
// A thread can also be opened by a retinue agent that needs a decision (via the
// gateway's token-gated /internal/conversations endpoint); such threads simply
// appear here with an unread badge and Ara engages once the user replies.
//
// The conversation itself — thread, composer, dictation, attachments, chips
// and copy buttons, model picker, read-aloud — is <retinue-conversation>
// (components/conversation.js). This card owns what is around it: the list,
// the Active/Archived/… filter, the location-hash routing that makes threads
// and the composer addressable, and the `data-view` attribute the page's
// styles key on. An open thread is that element with a `conversation-id`; the
// "+ New" composer is the same element with no id yet (and, coming from a
// project page, the project it is about) — its first message opens the thread
// and the element goes on as it.
//
// The element runs in two modes. By default it is a compact dashboard card that
// shows the most recent active threads (capped at MAX_CARD_THREADS) plus a link
// to the dedicated all-conversations page, so the dashboard stays uncluttered.
// With the `full` attribute (used on conversations.html) it shows every thread
// with an Active/Archived filter and no cap.
//
// Everything degrades gracefully offline (the list just fails to refresh; the
// last rendered state stays on screen).

import {
  esc, fmtAge, isWideFrame, onFrameChange,
  viewPref, setViewPref, viewToggleHtml, VIEW_TOGGLE_CSS,
} from './base.js';
import { hasUnsentInput } from './conversation.js';

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

class RetinueConversations extends HTMLElement {
  constructor() {
    super();
    this._threads = [];     // list summaries
    this._active = null;    // id of the open thread, or null for the list view
    this._composing = false; // true while the "new thread" composer is open
    this._timer = null;
    this._listSig = '';
    this._lastMode = '';
    this._full = false;      // full mode: dedicated all-conversations page
    this._scope = 'active';  // full-mode thread filter: active|archived|edits|cowork
    this._composeProject = null;      // project URI the composer is about, if any
    this._composeProjectTitle = '';   // its display title (for the chip)
    this._pushDepth = 0;     // history entries we pushed and have not unwound
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
    // Crossing the layout breakpoint changes how many threads fit (see
    // _shownThreads), so re-render when it flips.
    this._offFrame = onFrameChange(() => { if (!this._full) this.render(); });
    // What the open conversation tells this card. The events bubble out of
    // the element (and out of the read-aloud bar in the list), so one set of
    // listeners on the host covers every render — and every connection:
    // listeners on the host outlive a disconnect, so they go on once.
    if (!this._listening) {
      this._listening = true;
      this.addEventListener('retinue-back', () => this._openList());
      this.addEventListener('retinue-created', (e) => this._onCreated(e.detail || {}));
      this.addEventListener('retinue-archived', () => { this._openList(); this.refresh(); });
      this.addEventListener('retinue-sent', () => this.refresh());
      this.addEventListener('retinue-open', (e) => {
        const id = e.detail && e.detail.id;
        if (id) this._openThread(id);
      });
    }
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
    if (this._offFrame) this._offFrame();
    this._offFrame = null;
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
      // The project context is snapshotted into the element's attributes at
      // render time, so a change of context while composing re-renders.
      const was = `${this._composeProject || ''}\n${this._composeProjectTitle}`;
      this._setComposeProject(cm[1]);
      const now = `${this._composeProject || ''}\n${this._composeProjectTitle}`;
      if (!this._composing) this._showComposer();
      else if (now !== was) this.render();
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

  // True while a page reload would lose in-memory user input — a draft kept
  // for any thread (or the composer) even after leaving it, or files picked
  // for upload. components/update.js consults this before auto-reloading into
  // a freshly activated shell version.
  get dirty() {
    return hasUnsentInput();
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
      // In place: a full render would tear down the open conversation.
      this._partialUpdate();
    } catch (_err) {
      // Offline or gateway down: keep the last rendered state.
    }
  }

  // Apply a list refresh (badge counts, previews, new threads) without
  // rebuilding the shadow DOM. The open conversation keeps itself current.
  _partialUpdate() {
    const root = this.shadowRoot;
    if (!root) return;
    // If we're in a structurally different view than last full render, fall
    // back to a full render so the right widgets exist to update in place.
    const mode = this._active ? 'thread' : (this._composing ? 'composer' : 'list');
    if (mode !== this._lastMode) { this.render(); return; }
    if (mode !== 'list') return;

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
  }

  // Switch the full-page thread filter and reload that scope.
  _setScope(scope) {
    if (this._scope === scope) return;
    this._scope = scope;
    this._threads = [];
    this.render();
    this.refresh();
  }

  // The composer's first message opened a thread: the element already went on
  // as that thread, so only this card's own state and the address bar move.
  // The thread reuses the composer's history entry, so back still lands on
  // the list rather than the (now gone) composer.
  _onCreated(detail) {
    const id = detail.id;
    if (!id) return;
    history.replaceState(null, '', `#conversation-${id}`);
    this._active = id;
    this._composing = false;
    this._setComposeProject(undefined); // link consumed by this thread
    this._lastMode = 'thread';
    this.setAttribute('data-view', 'thread');
    this.refresh();
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
    this.render();
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
    this._composing = true;
    this.render();
  }

  render() {
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
    this.shadowRoot.innerHTML = `<style>${CSS}${VIEW_TOGGLE_CSS}</style>` +
      `<section class="card">${header}<div class="content">${body}</div></section>`;
    this._lastMode = mode;
    this._listSig = this._lastMode === 'list' ? this._listSignature() : '';
    this._wire();
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

  // An open thread: the conversation element, with a back button that this
  // card answers by unwinding the history entry it pushed.
  _threadView() {
    return `<retinue-conversation conversation-id="${esc(this._active)}" back></retinue-conversation>`;
  }

  // The new-thread composer: the same element with no thread yet. Coming from
  // a project page, the project it is about rides along as the seed of the
  // thread its first message opens.
  _composerView() {
    const project = this._composeProject
      ? ` for-project="${esc(this._composeProject)}" project-title="${esc(this._composeProjectTitle)}"`
      : '';
    return `<retinue-conversation back${project}></retinue-conversation>`;
  }

  _listView() {
    // A new thread is always an active chat, so the composer would be
    // confusing while the Archived or Edits filter is showing — hide it there.
    const newBtn = (this._full && this._scope !== 'active')
      ? '' : '<button class="new" data-new>+ New conversation with Ara</button>';
    // The tabs area takes all remaining height and scrolls; the New button and
    // page link stay pinned at the bottom, within thumb reach. The read-aloud
    // bar sits between them: a reading follows the user out of its thread.
    return this._filterHtml() +
      `<div class="tabs${this._view === 'list' ? ' as-list' : ''}">` +
      `${this._tabsHtml()}${this._emptyHtml()}</div>` +
      `<retinue-read-aloud></retinue-read-aloud>` +
      `<div class="list-foot">${newBtn}${this._footerHtml()}</div>`;
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

  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll('[data-open]').forEach((el) =>
      el.addEventListener('click', () => this._openThread(el.getAttribute('data-open'))));
    const nw = root.querySelector('[data-new]');
    if (nw) nw.addEventListener('click', () => this._openComposer());
    root.querySelectorAll('[data-scope]').forEach((el) =>
      el.addEventListener('click', () => this._setScope(el.getAttribute('data-scope'))));
    root.querySelectorAll('[data-setview]').forEach((el) =>
      el.addEventListener('click', () => {
        this._view = el.getAttribute('data-setview');
        setViewPref('conversations', this._view);
        this.render();
      }));
  }
}

const CSS = `
  :host { display: flex; flex-direction: column; min-height: 0; height: 100%; }
  * { box-sizing: border-box; }
  button { font: inherit; }
  button:focus-visible, a:focus-visible {
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
  /* The open conversation fills the card; it lays itself out inside. */
  retinue-conversation { flex: 1; min-height: 0; }

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
`;

customElements.define('retinue-conversations', RetinueConversations);
