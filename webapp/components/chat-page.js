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
// FIXTURE MODE: everything renders from the static documents under
// /data/chats/ (shape = the chat API contract draft, see webapp/README.md,
// "Messenger chats (fixture)"). Sending appends locally and says so; nothing
// leaves the browser. The later phases swap the data source for the live chat
// API and wire the composer to the real send path — the rendering below is the
// part meant to survive that swap.
//
// The companion pane deliberately does NOT embed components/conversations.js:
// that element owns location.hash routing (#conversation-…), polls the
// /conversations endpoints, and flags itself via data-view so styles.css hides
// the rest of the page — none of which can host a side-by-side pane without
// refactoring it. Instead this pane replicates the conversation thread's
// visual language (same bubble classes and styles, the shared Markdown
// renderer, so both render identically) over fixture messages; phase 4 points
// it at the chat's real companion thread (conversation kind `companion`)
// through the /conversations API — or at a thread view extracted from
// conversations.js by then.

import { esc, WIDE_FRAME } from './base.js';
import { renderMarkdown, MD_CSS } from './markdown.js';
import { avatarHtml, colorFor, CHANNELS } from './chats.js';

const LIST_URL = '/data/chats.json';
// Splitter persistence, per device — same pattern as layout.js (STORE_KEY).
const STORE_KEY = 'retinue.chatpage.v1';
const MIN_COMP_PX = 280;      // keep in sync with .pane-companion min-width
const MAX_COMP_FRACTION = 0.6;
const KEY_STEP_PX = 32;
const TEXTAREA_MAX_HEIGHT_RATIO = 0.35;
const NOTE_MS = 2600;

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

function fmtTime(iso) {
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? ''
    : t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function dayKey(iso) {
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? '' : t.toDateString();
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
    this._companion = [];      // fixture companion-thread messages
    this._pane = 'chat';       // chat | companion (phone pane indicator)
    this._draft = '';          // chat composer text
    this._draftByAra = false;  // composer holds the staged agent draft
    this._compDraft = '';      // companion composer text
    this._localSeq = 0;        // ids for locally appended fixture messages
    this._noteTimer = null;
    this._sizes = this._loadSizes();
    this._wide = matchMedia(WIDE_FRAME);
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._id = new URLSearchParams(location.search).get('id') || '';
    // Pane arrangement differs across the breakpoint; re-render on a flip
    // (drafts survive — they live in fields, mirrored on every input event).
    this._onFrame = () => this.render();
    this._wide.addEventListener('change', this._onFrame);
    this.render();
    this._load();
  }

  disconnectedCallback() {
    this._wide.removeEventListener('change', this._onFrame);
    if (this._noteTimer) clearTimeout(this._noteTimer);
  }

  async _load() {
    if (!this._id) {
      this._state = 'error';
      this._error = 'No chat id in the address.';
      this.render();
      return;
    }
    try {
      // Two hops, both part of the contract: the list document names each
      // chat's message document in `messages`, so the client never builds a
      // message URL itself — the fixture→API swap changes only these payloads.
      const listRes = await fetch(LIST_URL, { cache: 'no-store' });
      if (!listRes.ok) throw new Error(String(listRes.status));
      const list = await listRes.json();
      const summary = (list.chats || []).find((c) => c.id === this._id);
      if (!summary || !summary.messages) {
        this._state = 'error';
        this._error = 'This chat is not in the fixture data.';
        this.render();
        return;
      }
      const msgRes = await fetch(summary.messages, { cache: 'no-store' });
      if (!msgRes.ok) throw new Error(String(msgRes.status));
      const doc = await msgRes.json();
      this._chat = doc.chat || summary;
      this._messages = Array.isArray(doc.messages) ? doc.messages : [];
      this._companion = Array.isArray(doc.companion) ? doc.companion : [];
      // A staged draft lands in the composer, marked with its author — the
      // agent writes into the draft, the user's send button sends.
      const draft = this._chat.draft;
      if (draft && draft.text && !this._draft) {
        this._draft = draft.text;
        this._draftByAra = draft.author === 'agent';
      }
      this._state = 'ok';
      try { document.title = `Retinue — ${this._chat.name}`; } catch (_e) { /* ignore */ }
      this.render();
    } catch (_err) {
      this._state = 'error';
      this._error = 'Could not load this chat. Offline?';
      this.render();
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
        '<p><a class="backlink" href="/chats.html">&#8249; All chats</a></p></div>';
    } else {
      body = this._headHtml() + this._panesHtml();
    }
    this.shadowRoot.innerHTML = `<style>${CSS}${MD_CSS}</style>` +
      `<section class="page">${body}</section>`;
    if (this._state === 'ok') {
      this._applySizes();
      this._wire();
      this._scrollThread('[data-chat-thread]');
      this._scrollThread('[data-comp-thread]');
      this._setPane(this._pane, 'instant');
    }
  }

  _headHtml() {
    const c = this._chat;
    const ch = esc((CHANNELS[c.channel] || { label: c.channel }).label);
    const key = c.id.slice(c.id.indexOf(':') + 1);
    const sub = c.group
      ? `${ch} group${c.members ? ` &middot; ${Number(c.members)} members` : ''}`
      : `${ch} &middot; ${esc(key)}`;
    return `<header class="chat-head">` +
      `<a class="back" href="/chats.html" aria-label="All chats">&#8249;</a>` +
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
      `<div class="comp-bar"><span class="comp-who">Ara</span>` +
      `<span class="comp-hint">reads this chat, writes into your draft</span></div>` +
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
    const firstUnread = unread ? msgs.length - unread : -1;
    let html = '';
    let lastDay = '';
    msgs.forEach((m, i) => {
      const day = dayKey(m.ts);
      if (day !== lastDay) {
        html += `<div class="day-sep"><span>${esc(fmtDay(m.ts))}</span></div>`;
        lastDay = day;
      }
      if (i === firstUnread) {
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
    const media = (m.attachments || []).map((a) => {
      if (!String(a.type || '').startsWith('image/')) {
        return `<span class="att-file">&#128206; ${esc(a.name || 'attachment')}</span>`;
      }
      // The contract carries the image's intrinsic size so the box is reserved
      // before the bytes arrive — a lazily loading image must never shift the
      // thread's scroll position.
      const dims = (a.width > 0 && a.height > 0)
        ? ` width="${Number(a.width)}" height="${Number(a.height)}"` : '';
      return `<img class="att-img" src="${esc(a.url)}" alt="${esc(a.name || 'image')}"` +
        `${dims} loading="lazy">`;
    }).join('');
    const text = m.text ? `<span class="txt">${linkify(m.text)}</span>` : '';
    return `<div class="${cls}">${head}<div class="bubble">${media}${text}` +
      `<span class="stamp">${esc(fmtTime(m.ts))}</span></div></div>`;
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
    // Row order is deliberate: the clear ✕ sits on the far LEFT, send on the
    // far right — the ✕ must never be one fat finger away from send (the same
    // edge the recording row's discard/send split relies on).
    return `<div class="composer">` +
      `<div class="chips" role="toolbar" aria-label="Quick patterns">${chips}</div>` +
      `<div class="note" data-note role="status" hidden></div>` +
      draftTag +
      `<form class="row" data-chat-form>` +
      `<button type="button" class="clearbtn" data-clear title="Clear message" ` +
      `aria-label="Clear message" ${this._draft ? '' : 'disabled'}>&#10005;</button>` +
      `<textarea rows="1" placeholder="Message &#8230;" aria-label="Message" ` +
      `autocomplete="off">${esc(this._draft)}</textarea>` +
      `<button type="button" class="mic" disabled ` +
      `title="Voice input arrives with the live chat API" ` +
      `aria-label="Voice input arrives with the live chat API">&#127908;</button>` +
      `<button type="submit" class="send" title="Send" aria-label="Send">&#10148;</button>` +
      `</form></div>`;
  }

  // Companion messages reuse the conversation thread's visual language (same
  // .msg/.bubble classes and styles as conversations.js, Markdown via the
  // shared renderer) so this pane and real dashboard conversations render
  // identically by construction.
  _companionThreadHtml() {
    if (!this._companion.length) {
      return `<div class="center muted comp-empty"><span class="e-ico" aria-hidden="true">&#x1F4AC;</span>` +
        `<p>Ask Ara about this chat &mdash; she reads it and stages replies into your draft.</p></div>`;
    }
    return this._companion.map((m) => this._companionMsgHtml(m)).join('');
  }

  _companionMsgHtml(m) {
    const me = m.role === 'user';
    const who = me ? 'You' : 'Ara';
    return `<div class="cmsg${me ? ' me' : ''}">` +
      `<div class="cmsg-head"><small class="who">${who}</small>` +
      `<small class="cmeta">${esc(fmtTime(m.ts))}</small></div>` +
      `<div class="cbubble">${renderMarkdown(m.text)}</div></div>`;
  }

  _companionComposerHtml() {
    return `<div class="composer">` +
      `<form class="row" data-comp-form>` +
      `<textarea rows="1" placeholder="Ask Ara &#8230;" aria-label="Ask Ara" ` +
      `autocomplete="off">${esc(this._compDraft)}</textarea>` +
      `<button type="submit" class="send" title="Send" aria-label="Send">&#10148;</button>` +
      `</form></div>`;
  }

  // ── Wiring ─────────────────────────────────────────────────────────────────
  _wire() {
    const root = this.shadowRoot;
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

    // Chat composer.
    const form = root.querySelector('[data-chat-form]');
    if (form) {
      const input = form.querySelector('textarea');
      const clearBtn = form.querySelector('[data-clear]');
      const grow = () => {
        input.style.height = 'auto';
        input.style.height =
          `${Math.min(input.scrollHeight, Math.round(window.innerHeight * TEXTAREA_MAX_HEIGHT_RATIO))}px`;
      };
      input.addEventListener('input', () => {
        this._draft = input.value;
        clearBtn.disabled = !input.value;
        grow();
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          form.requestSubmit();
        }
      });
      grow();
      clearBtn.addEventListener('click', () => {
        // One tap to an empty box — the deliberate divergence from the real
        // clients. It also dismisses the staged-draft marker: the draft is
        // rejected, not sent.
        input.value = '';
        this._draft = '';
        this._setDraftByAra(false);
        clearBtn.disabled = true;
        grow();
        input.focus();
      });
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        const text = input.value;
        if (!text.trim()) return;
        this._sendChat(text);
        input.value = '';
        this._draft = '';
        this._setDraftByAra(false);
        clearBtn.disabled = true;
        grow();
      });
    }

    // Quick patterns: pre-fill the companion composer with the canned prompt
    // and bring that pane forward — a chip is a companion turn, nothing more.
    root.querySelectorAll('[data-quick]').forEach((el) =>
      el.addEventListener('click', () => this._quickPattern(el.getAttribute('data-quick'))));

    // Companion composer (fixture: echoes locally, then a canned notice).
    const compForm = root.querySelector('[data-comp-form]');
    if (compForm) {
      const input = compForm.querySelector('textarea');
      const grow = () => {
        input.style.height = 'auto';
        input.style.height =
          `${Math.min(input.scrollHeight, Math.round(window.innerHeight * TEXTAREA_MAX_HEIGHT_RATIO))}px`;
      };
      input.addEventListener('input', () => { this._compDraft = input.value; grow(); });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          compForm.requestSubmit();
        }
      });
      grow();
      compForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const text = input.value;
        if (!text.trim()) return;
        this._sendCompanion(text);
        input.value = '';
        this._compDraft = '';
        grow();
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

  _setDraftByAra(on) {
    this._draftByAra = on;
    const tag = this.shadowRoot.querySelector('[data-draft-tag]');
    if (tag && !on) tag.remove();
  }

  _scrollThread(sel) {
    const el = this.shadowRoot.querySelector(sel);
    if (el) el.scrollTop = el.scrollHeight;
  }

  // Fixture send: optimistic local echo only. The real path (phase 3) POSTs to
  // the chat send endpoint, the gateway records author `user`, and no policy
  // category queues it — the user's send button IS the approval.
  _sendChat(text) {
    this._localSeq += 1;
    const m = {
      id: `local-${this._localSeq}`,
      chat: this._chat.id,
      direction: 'out',
      author: 'user',
      text,
      ts: new Date().toISOString(),
    };
    this._messages.push(m);
    this._appendMessage('[data-chat-thread]', m, true);
    this._showNote('Fixture mode &mdash; message not sent anywhere.');
  }

  _appendMessage(sel, m, isChat) {
    const thread = this.shadowRoot.querySelector(sel);
    if (!thread) return;
    const prev = isChat
      ? [...this._messages].reverse().find((x) => x !== m)
      : this._companion[this._companion.length - 2];
    let html = '';
    if (isChat && (!prev || dayKey(prev.ts) !== dayKey(m.ts))) {
      html += `<div class="day-sep"><span>${esc(fmtDay(m.ts))}</span></div>`;
    }
    html += isChat ? this._chatMsgHtml(m) : this._companionMsgHtml(m);
    thread.insertAdjacentHTML('beforeend', html);
    thread.scrollTop = thread.scrollHeight;
  }

  _showNote(html) {
    const note = this.shadowRoot.querySelector('[data-note]');
    if (!note) return;
    note.innerHTML = html;
    note.hidden = false;
    // The note row grows the composer at the thread's expense; keep the
    // newest bubble in view through both height changes.
    this._scrollThread('[data-chat-thread]');
    if (this._noteTimer) clearTimeout(this._noteTimer);
    this._noteTimer = setTimeout(() => {
      note.hidden = true;
      this._scrollThread('[data-chat-thread]');
    }, NOTE_MS);
  }

  _quickPattern(id) {
    const p = QUICK_PATTERNS.find((x) => x.id === id);
    if (!p) return;
    const prompt = p.prompt(this._draft.trim());
    const compForm = this.shadowRoot.querySelector('[data-comp-form]');
    const input = compForm && compForm.querySelector('textarea');
    if (input) {
      input.value = prompt;
      this._compDraft = prompt;
      input.style.height = 'auto';
      input.style.height =
        `${Math.min(input.scrollHeight, Math.round(window.innerHeight * TEXTAREA_MAX_HEIGHT_RATIO))}px`;
    }
    this._setPane('companion', 'smooth');
    if (input) setTimeout(() => { try { input.focus(); } catch (_e) { /* ignore */ } }, 320);
  }

  // Fixture companion turn: echo the user's message, then answer with the one
  // honest reply a fixture can give. Phase 4 replaces both with a real turn in
  // the chat's companion thread.
  _sendCompanion(text) {
    const m = { role: 'user', text, ts: new Date().toISOString() };
    // A first message replaces the empty-state hint.
    if (!this._companion.length) {
      const t = this.shadowRoot.querySelector('[data-comp-thread]');
      if (t) t.innerHTML = '';
    }
    this._companion.push(m);
    this._appendMessage('[data-comp-thread]', m, false);
    setTimeout(() => {
      const reply = {
        role: 'ara',
        text: 'Fixture mode: this pane is not wired to an agent yet. Once the chat API exists, ' +
          'this turn reaches Ara with the chat context and the shared draft.',
        ts: new Date().toISOString(),
      };
      this._companion.push(reply);
      this._appendMessage('[data-comp-thread]', reply, false);
    }, 500);
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
  .att-img { display: block; max-width: min(280px, 100%); height: auto;
             border-radius: 10px; margin: 2px 0 6px; }
  .att-file { display: block; font-size: .82rem; margin: 2px 0 4px; }

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
  .row textarea { flex: 1; min-width: 0; min-height: 40px; max-height: 35vh;
                  background: var(--card-2, #1c2230); border: 0; border-radius: 20px;
                  padding: 9px 14px; color: var(--fg, #e7ebf2); font: inherit; line-height: 1.35;
                  resize: none; overflow-y: auto; }
  .row textarea::placeholder { color: var(--muted, #8b93a3); }
  .row textarea:focus-visible { outline: 1px solid rgba(110, 168, 254, .45); outline-offset: 0; }
  .row button { flex: none; display: inline-flex; align-items: center; justify-content: center;
                width: 40px; height: 40px; border-radius: 50%; border: 0; font-size: 1.05rem;
                cursor: pointer; padding: 0; -webkit-tap-highlight-color: transparent; }
  .send { background: var(--accent, #6ea8fe); color: #0b0d12; padding-left: 2px; }
  .send:active { filter: brightness(1.12); }
  .clearbtn { background: var(--card-2, #1c2230); color: var(--high, #ff6b6b); }
  .clearbtn:hover { background: rgba(255, 107, 107, .18); }
  .clearbtn[disabled] { opacity: .4; cursor: default; }
  .clearbtn[disabled]:hover { background: var(--card-2, #1c2230); }
  .mic { background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); }
  .mic[disabled] { opacity: .5; cursor: default; }

  /* ── Companion pane (the conversation thread's visual language) ──────────── */
  .cmsg { display: flex; flex-direction: column; gap: 3px; max-width: 86%; align-self: flex-start; }
  .cmsg.me { align-self: flex-end; align-items: flex-end; }
  .cmsg-head { display: flex; align-items: center; gap: 6px; }
  .cmsg.me .cmsg-head { flex-direction: row-reverse; }
  .cmeta { color: var(--muted, #8b93a3); font-size: .7rem; }
  .cbubble { background: var(--card-2, #1c2230); border-radius: 16px;
             border-bottom-left-radius: 6px; padding: 9px 13px; line-height: 1.4; }
  .cmsg.me .cbubble { background: var(--accent, #6ea8fe); color: #0b0d12;
                      border-bottom-left-radius: 16px; border-bottom-right-radius: 6px; }
  .cmsg.me .cbubble .md a { color: #0b0d12; }
  .comp-empty .e-ico { font-size: 2rem; opacity: .55; }
  .comp-empty p { margin: 0; max-width: 32ch; }
`;

customElements.define('retinue-chat-page', RetinueChatPage);
