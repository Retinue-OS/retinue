// <retinue-shelf>: the minimized pages (issue #282) — threads, chats and
// projects parked like windows on a desktop's taskbar, one tap from where
// they were left. Every page carries one, as the last child of <main>.
//
// What is on it, and every rule about when things come and go, lives in
// shelf-store.js; this element only draws the state and turns taps into
// navigation. It also keeps the state honest for items no page in this tab is
// showing: a light reconciliation read (thread documents, the chat list) while
// the tab is visible — the pages that show an item feed it themselves as they
// read, so the shelf never becomes a second, independent source of truth.
//
// Markers — two corners, two voices:
//   top right     the people: a chat's unread count (filled blue)
//   bottom right  Ara: ··· while she works (static, outlined), a green dot
//                 once she has answered. When the answer lands, the dots
//                 gather into the dot once — the ellipsis becomes a full
//                 stop, the only motion here.
//
// Phone: one compact row of icons, no labels (each has an accessible name;
// the title shows in the long-press menu), scrolling sideways when it is
// longer than the screen. Wide frame: a taskbar of chips with the title and
// the state in words.
//
// Long press, right-click or the context-menu key: Open · Close · Close all
// quiet ones.

import { esc, WIDE_FRAME, onPressOutside } from './base.js';
import { shelf, subscribe, readState, isLive, isQuiet, setRestore, threadFacts } from './shelf-store.js';

// How often items no open page is watching are re-read: tight while one of
// them is waiting on Ara, relaxed otherwise, never while the tab is hidden.
const WORKING_POLL_MS = 4000;
const IDLE_POLL_MS = 20000;
const LONG_PRESS_MS = 500;
// How long the dots take to gather into the full stop.
const SETTLE_MS = 450;

const ICON_THREAD = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
const ICON_PROJECT = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';

function stateWords(it) {
  const parts = [];
  if (it.araState === 'working') parts.push('Ara at work');
  if (it.araState === 'ready') parts.push('Ara has answered');
  if (it.unread > 0) parts.push(`${it.unread} new message${it.unread === 1 ? '' : 's'}`);
  return parts;
}

function kindWord(kind) {
  return kind === 'chat' ? 'Chat' : kind === 'project' ? 'Project' : 'Thread';
}

class RetinueShelf extends HTMLElement {
  constructor() {
    super();
    this._prev = new Map();   // key → araState last drawn (for the one-time settle)
    this._menuKey = '';
    this._menuAway = null;
    this._pollTimer = null;
    this._polling = false;
    this._suppressClick = false;
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._wide = matchMedia(WIDE_FRAME);
    this._onFrame = () => this.render();
    this._wide.addEventListener('change', this._onFrame);
    // A change can bring an item that needs reading sooner than the timer
    // armed before it (a thread just left while Ara works): re-arm, earlier only.
    this._off = subscribe(() => { this.render(); this._schedule(true); });
    this._onVis = () => { if (!document.hidden) this._reconcile(); };
    document.addEventListener('visibilitychange', this._onVis);
    this.render();
    this._reconcile();
  }

  disconnectedCallback() {
    if (this._off) this._off();
    this._off = null;
    if (this._wide) this._wide.removeEventListener('change', this._onFrame);
    document.removeEventListener('visibilitychange', this._onVis);
    if (this._pollTimer) clearTimeout(this._pollTimer);
    this._pollTimer = null;
    this._closeMenu();
  }

  _items() {
    return readState().items.slice().sort((a, b) => a.added - b.added);
  }

  // ── Drawing ────────────────────────────────────────────────────────────────
  render() {
    const root = this.shadowRoot;
    if (!root) return;
    const items = this._items();
    this.hidden = !items.length;
    // Keep the row's sideways scroll across redraws (a marker changing must
    // not throw the user back to the start of a long shelf).
    const row = root.querySelector('.row');
    const scroll = row ? row.scrollLeft : 0;
    const wide = this._wide && this._wide.matches;
    const settling = new Set();
    for (const it of items) {
      if (this._prev.get(it.key) === 'working' && it.araState === 'ready') settling.add(it.key);
    }
    this._prev = new Map(items.map((it) => [it.key, it.araState]));
    root.innerHTML = `<style>${CSS}</style>` +
      `<nav class="row${wide ? ' wide' : ''}" aria-label="Minimized pages">` +
      items.map((it) => this._itemHtml(it, wide, settling.has(it.key))).join('') +
      `</nav>` + this._menuHtml(items);
    const again = root.querySelector('.row');
    if (again) again.scrollLeft = scroll;
    this._wire();
    if (settling.size) {
      setTimeout(() => {
        root.querySelectorAll('.settle').forEach((el) => el.classList.remove('settle'));
      }, SETTLE_MS + 50);
    }
  }

  _iconHtml(it) {
    if (it.kind === 'chat') {
      const av = it.avatar || {};
      const color = /^#[0-9a-f]{3,8}$/i.test(av.color || '') ? av.color : '#3a4a6b';
      return `<span class="ico av" style="background:${color}" aria-hidden="true">` +
        `${esc(av.text || (it.title || '?').slice(0, 1).toUpperCase())}</span>`;
    }
    return `<span class="ico ${it.kind === 'project' ? 'proj' : 'thr'}" aria-hidden="true">` +
      `${it.kind === 'project' ? ICON_PROJECT : ICON_THREAD}</span>`;
  }

  _markersHtml(it, settle) {
    const count = it.unread > 0
      ? `<span class="count" aria-hidden="true">${it.unread > 99 ? '99+' : Number(it.unread)}</span>` : '';
    let ara = '';
    if (it.araState === 'working') {
      ara = '<span class="ara working" aria-hidden="true"><i></i><i></i><i></i></span>';
    } else if (it.araState === 'ready') {
      ara = `<span class="ara ready${settle ? ' settle' : ''}" aria-hidden="true"><i></i><i></i><i></i></span>`;
    }
    return count + ara;
  }

  _itemHtml(it, wide, settle) {
    const words = stateWords(it);
    const title = it.title || kindWord(it.kind);
    const label = [title, kindWord(it.kind).toLowerCase(), ...words].join(', ');
    const current = isLive(it.key) ? ' aria-current="page"' : '';
    const text = wide
      ? `<span class="t">${esc(title)}</span>` +
        (words.length ? `<span class="s ${it.araState}">${esc(words.join(' · '))}</span>` : '')
      : '';
    return `<a class="it" href="${esc(it.href || '/')}" data-key="${esc(it.key)}"${current} ` +
      `aria-label="${esc(label)}" title="${esc(title)}">` +
      `<span class="badge">${this._iconHtml(it)}${this._markersHtml(it, settle)}</span>${text}</a>`;
  }

  _menuHtml(items) {
    const it = items.find((i) => i.key === this._menuKey);
    if (!it) return '';
    const quiet = items.filter(isQuiet).length;
    return `<div class="menu" role="dialog" aria-label="${esc(it.title || kindWord(it.kind))}" data-menu>` +
      `<div class="mt">${esc(it.title || kindWord(it.kind))}</div>` +
      `<button type="button" data-m="open">Open</button>` +
      `<button type="button" data-m="close">Close</button>` +
      (quiet ? `<button type="button" data-m="quiet">Close all quiet ones</button>` : '') +
      `</div>`;
  }

  // ── Behaviour ──────────────────────────────────────────────────────────────
  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll('a.it').forEach((a) => {
      const key = a.getAttribute('data-key');
      a.addEventListener('click', (e) => {
        if (this._suppressClick) { e.preventDefault(); this._suppressClick = false; return; }
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
        e.preventDefault();
        this._open(key);
      });
      a.addEventListener('contextmenu', (e) => { e.preventDefault(); this._openMenu(key); });
      a.addEventListener('keydown', (e) => {
        if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) { e.preventDefault(); this._openMenu(key); }
      });
      let timer = null;
      let start = null;
      const cancel = () => { if (timer) clearTimeout(timer); timer = null; };
      a.addEventListener('pointerdown', (e) => {
        if (e.pointerType === 'mouse') return; // right-click is the mouse's way
        start = { x: e.clientX, y: e.clientY };
        timer = setTimeout(() => { timer = null; this._suppressClick = true; this._openMenu(key); }, LONG_PRESS_MS);
      });
      a.addEventListener('pointermove', (e) => {
        if (start && Math.hypot(e.clientX - start.x, e.clientY - start.y) > 8) cancel();
      });
      a.addEventListener('pointerup', cancel);
      a.addEventListener('pointercancel', cancel);
    });
    const menu = root.querySelector('[data-menu]');
    if (menu) {
      menu.querySelectorAll('[data-m]').forEach((b) => b.addEventListener('click', () => {
        const key = this._menuKey;
        const what = b.getAttribute('data-m');
        this._closeMenu(true);
        if (what === 'open') this._open(key);
        else if (what === 'close') shelf.close(key);
        else if (what === 'quiet') shelf.closeQuiet();
      }));
      const first = menu.querySelector('button');
      if (first) first.focus();
    }
  }

  _openMenu(key) {
    this._closeMenu();
    this._menuKey = key;
    this.render();
    const menu = this.shadowRoot.querySelector('[data-menu]');
    if (!menu) return;
    const onKey = (e) => { if (e.key === 'Escape') this._closeMenu(true); };
    const offPress = onPressOutside(menu, () => this._closeMenu(true), { capture: true });
    document.addEventListener('keydown', onKey);
    this._menuAway = () => { offPress(); document.removeEventListener('keydown', onKey); };
  }

  _closeMenu(redraw) {
    if (this._menuAway) this._menuAway();
    this._menuAway = null;
    const had = !!this._menuKey;
    this._menuKey = '';
    if (had && redraw) this.render();
  }

  // Restore: hand the item's place to the page that opens (per tab), then go
  // there. A thread whose page is this one opens in place, by its hash.
  _open(key) {
    const it = readState().items.find((i) => i.key === key);
    if (!it || isLive(key)) return;
    shelf.touch(key);
    setRestore(key, it.location || null);
    let url;
    try { url = new URL(it.href || '/', location.href); } catch (_e) { return; }
    const here = url.pathname === location.pathname && url.search === location.search;
    if (here && url.hash && document.querySelector('retinue-conversations')) {
      location.hash = url.hash;
      return;
    }
    location.href = url.href;
  }

  // ── Reconciliation ─────────────────────────────────────────────────────────
  // Items no page in this tab is showing are re-read here; the rest are fed
  // by their pages. Chats come from one list read; a chat's companion thread
  // and plain threads from their documents.
  _schedule(earlierOnly) {
    const items = readState().items.filter((i) => !isLive(i.key) && i.kind !== 'project');
    const busy = items.some((i) => i.araState === 'working');
    const due = Date.now() + (busy ? WORKING_POLL_MS : IDLE_POLL_MS);
    if (earlierOnly && (this._polling || (this._pollTimer && this._dueAt <= due))) return;
    if (this._pollTimer) clearTimeout(this._pollTimer);
    this._dueAt = due;
    this._pollTimer = setTimeout(() => { this._pollTimer = null; this._reconcile(); }, due - Date.now());
  }

  async _reconcile() {
    if (this._polling) return;
    if (document.hidden || !this.isConnected) { this._schedule(); return; }
    this._polling = true;
    try {
      const items = readState().items.filter((i) => !isLive(i.key) && i.kind !== 'project');
      const chats = items.filter((i) => i.kind === 'chat');
      let list = null;
      if (chats.length) {
        try {
          const res = await fetch('/chats', { cache: 'no-store' });
          if (res.ok) list = (await res.json()).chats || [];
        } catch (_e) { /* offline: keep what is known */ }
      }
      for (const it of items) {
        if (isLive(it.key)) continue;
        if (it.kind === 'thread') {
          await this._readThread(it.key, it.id);
        } else if (it.kind === 'chat' && list) {
          const c = list.find((x) => x.id === it.id);
          if (!c) { shelf.muted(it.key); continue; } // erased: nothing to go back to
          shelf.observe(it.key, { unread: Number(c.unread) || 0, muted: !!c.muted, title: c.name || it.title });
          if (c.companion) await this._readThread(it.key, c.companion, true);
        }
      }
    } finally {
      this._polling = false;
      this._schedule();
    }
  }

  async _readThread(key, id, companion) {
    try {
      const res = await fetch(`/conversations/${encodeURIComponent(id)}`, { cache: 'no-store' });
      if (res.status === 404) { if (!companion) shelf.muted(key); return; }
      if (!res.ok) return;
      const t = await res.json();
      if (isLive(key)) return; // a page took over meanwhile
      const f = threadFacts(t);
      // A companion's own mute flag is not the chat's; the chat list says that.
      if (companion) delete f.muted; else if (t.title) f.title = t.title;
      shelf.observe(key, f);
    } catch (_e) { /* offline: keep what is known */ }
  }
}

const CSS = `
  :host { display: block; }
  :host([hidden]) { display: none; }
  * { box-sizing: border-box; }
  .row { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; padding: 6px 4px 2px;
         -webkit-overflow-scrolling: touch; }
  .row::-webkit-scrollbar { display: none; }
  .it { position: relative; flex: none; display: flex; align-items: center; gap: 10px;
        min-width: 48px; min-height: 48px; justify-content: center; border-radius: 14px;
        color: var(--fg, #e7ebf2); text-decoration: none; -webkit-tap-highlight-color: transparent;
        -webkit-touch-callout: none; user-select: none; -webkit-user-select: none; }
  .it:focus-visible { outline: 2px solid var(--accent, #6ea8fe); outline-offset: 1px; }
  .it[aria-current="page"] { background: rgba(231, 235, 242, .07); }
  .badge { position: relative; width: 44px; height: 44px; display: flex; align-items: center;
           justify-content: center; flex: none; }
  .ico { width: 38px; height: 38px; display: flex; align-items: center; justify-content: center;
         font-weight: 650; font-size: .95rem; color: #fff; }
  .ico.thr { border-radius: 50%; background: #24324d; color: var(--accent, #6ea8fe); }
  .ico.proj { border-radius: 11px; background: #2d2a22; color: #f0a35e; }
  .ico.av { border-radius: 50%; }
  /* The people: top right. */
  .count { position: absolute; right: -3px; top: -1px; min-width: 19px; height: 19px; padding: 0 5px;
           border-radius: 10px; background: var(--accent, #6ea8fe); color: #0b0d12; font-size: .68rem;
           font-weight: 700; display: flex; align-items: center; justify-content: center;
           border: 2px solid var(--bg, #0b0d12); }
  /* Ara: bottom right. Working is outlined and still; ready is a filled dot. */
  .ara { position: absolute; right: -4px; bottom: 0; height: 17px; border-radius: 9px;
         border: 2px solid var(--bg, #0b0d12); display: flex; align-items: center; justify-content: center;
         gap: 2px; }
  .ara i { width: 3.5px; height: 3.5px; border-radius: 50%; background: var(--accent, #6ea8fe); }
  .ara.working { padding: 0 4px; background: var(--card-2, #1c2230);
                 box-shadow: inset 0 0 0 1px rgba(110, 168, 254, .6); }
  .ara.ready { width: 15px; height: 15px; right: -1px; bottom: 1px; background: var(--ok, #57c785); }
  .ara.ready i { display: none; }
  /* The ellipsis becomes a full stop — once, when the answer lands. */
  .ara.ready.settle { animation: settle ${SETTLE_MS}ms ease-out both; }
  .ara.ready.settle i { display: block; animation: gather ${SETTLE_MS}ms ease-in both; }
  .ara.ready.settle i:first-child { --dx: 5.5px; }
  .ara.ready.settle i:last-child { --dx: -5.5px; }
  @keyframes settle {
    from { width: 25px; height: 17px; right: -4px; bottom: 0; background: var(--card-2, #1c2230);
           box-shadow: inset 0 0 0 1px rgba(110, 168, 254, .6); }
    to   { width: 15px; height: 15px; right: -1px; bottom: 1px; background: var(--ok, #57c785);
           box-shadow: none; }
  }
  @keyframes gather { to { transform: translateX(var(--dx, 0)); opacity: 0; } }
  @media (prefers-reduced-motion: reduce) {
    .ara.ready.settle, .ara.ready.settle i { animation: none; }
    .ara.ready.settle i { display: none; }
  }
  /* The wide frame: a taskbar of chips with words. */
  .row.wide { gap: 8px; padding: 8px 0 0; border-top: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .row.wide .it { justify-content: flex-start; padding: 0 14px 0 4px; background: var(--card, #151922);
                  border: 1px solid var(--line, rgba(231, 235, 242, .08)); max-width: 280px; }
  .row.wide .it[aria-current="page"] { border-color: var(--accent, #6ea8fe); }
  .row.wide .badge { width: 40px; height: 40px; }
  .row.wide .ico { width: 30px; height: 30px; font-size: .8rem; }
  .row.wide .ico svg { width: 17px; height: 17px; }
  .t { font-size: .88rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
       min-width: 0; }
  .s { font-size: .75rem; color: var(--muted, #8b93a3); white-space: nowrap; }
  .s.ready { color: var(--ok, #57c785); font-weight: 600; }
  .menu { position: absolute; bottom: calc(100% + 6px); left: 8px; min-width: 210px; max-width: calc(100% - 16px);
          padding: 6px; background: var(--card-2, #1c2230); border: 1px solid rgba(231, 235, 242, .14);
          border-radius: 14px; box-shadow: 0 16px 40px rgba(0, 0, 0, .6); display: flex;
          flex-direction: column; gap: 2px; z-index: 5; }
  .mt { padding: 8px 10px 6px; font-size: .78rem; color: var(--muted, #8b93a3); overflow: hidden;
        text-overflow: ellipsis; white-space: nowrap; }
  .menu button { text-align: left; min-height: 42px; padding: 0 10px; border: 0; border-radius: 8px;
                 background: transparent; color: var(--fg, #e7ebf2); font: inherit; font-size: .92rem; }
  .menu button:hover, .menu button:focus-visible { background: rgba(231, 235, 242, .08); outline: none; }
`;

customElements.define('retinue-shelf', RetinueShelf);
