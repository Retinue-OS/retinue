// Swipeable rows: the one gesture the chats and threads lists share, so the
// two can never drift apart.
//
// A row is swiped, messenger-style, rather than carrying buttons that eat a
// third of a phone's width:
//
//   swipe right  the row's quick action at once — out of the list (Archive)
//                or back into it (Restore), whichever the row's state calls for
//   swipe left   reveal the tray: Star · Archive · Mute, or
//                Star · Restore · Mute (Unmute) · Delete
//
// A long press, a right-click or the context-menu key opens the same tray
// without swiping. The gesture is claimed only once it is clearly horizontal,
// so a scroll that starts on a row is still a scroll.
//
// Two variations are the row's to opt into, so a list that wants neither (the
// threads page) keeps the gesture above exactly:
//
//   data-quick-left="<action>"  a swipe left COMMITS that action at once, past
//                the same threshold as a swipe right, over a layer of its own
//                docked to the right edge (.quick-left). The tray is then
//                reached only by long press, right-click or the context-menu
//                key — which is the chats page's Unread and Read tabs:
//                right marks read, left archives.
//   no data-quick               the row does not move to the right at all:
//                there is nothing to commit on that side (a chat already read).
//
// Markup a component renders for each row (the tray's buttons sit beside the
// row, never inside it — a row is a link or a button, and a button within one
// is not a button):
//
//   <div|li class="swipe" data-id="…" data-quick="archive|restore|…"
//           [data-quick-left="archive|…"]>
//     <div class="quick archive|restore" aria-hidden="true">Archive</div>
//     [<div class="quick-left archive" aria-hidden="true">Archive</div>]
//     <div class="tray"><button data-act="…">…</button>…</div>
//     <a|button class="row" draggable="false">…</a|button>
//   </div|li>
//
// The component keeps what the actions mean; this module keeps how a row
// moves. It also holds the two-tap confirm a Delete needs, and the Undo toast
// a commit that takes a row out of view offers (the home list's, UNDO_MS).

import { onPressOutside } from './base.js';

export const UNDO_MS = 6000;

// The CSS a swipeable list needs, for the component's shadow root. Rows bring
// their own look; this is only the layering and the tray's colours.
export const SWIPE_CSS = `
  .swipe { position: relative; overflow: hidden; }
  .swipe > .row { position: relative; z-index: 1; touch-action: pan-y; user-select: none;
                  -webkit-user-select: none; -webkit-touch-callout: none; }
  .swipe.busy > .row { opacity: .5; }
  /* visibility, not just the row on top: a hidden tray's buttons also leave
     the tab order, and an open one's are all reachable by Tab. */
  .swipe > .quick, .swipe > .quick-left, .swipe > .tray { position: absolute; top: 0; bottom: 0;
                                    display: flex; align-items: stretch; visibility: hidden; }
  .swipe.show-quick > .quick, .swipe.show-quick-left > .quick-left,
  .swipe.show-tray > .tray { visibility: visible; }
  .swipe > .quick, .swipe > .quick-left { left: 0; right: 0; align-items: center;
                    font-size: .8rem; font-weight: 600; color: #0b0d12; }
  .swipe > .quick { padding-left: 18px; }
  /* The left swipe's layer says its action at the edge the row uncovers. */
  .swipe > .quick-left { justify-content: flex-end; padding-right: 18px; }
  .quick.archive, .quick-left.archive { background: var(--accent, #6ea8fe); }
  .quick.restore { background: #4f9e63; }
  .quick.read { background: #3fa7a0; }
  .swipe > .tray { right: 0; }
  .tray button { border: 0; margin: 0; padding: 0 11px; min-width: 60px; cursor: pointer;
                 font: inherit; font-size: .76rem; font-weight: 600; color: #0b0d12;
                 white-space: nowrap; -webkit-tap-highlight-color: transparent; }
  .tray button:focus-visible { outline: 2px solid var(--fg, #e7ebf2); outline-offset: -3px; }
  /* Star does not take the row out of the list, so it is the quiet one — and
     no amber, which is Mute's. */
  .tray .star, .tray .unstar { background: var(--muted, #8b93a3); }
  .tray .archive { background: var(--accent, #6ea8fe); }
  .tray .read { background: #3fa7a0; }
  .tray .restore { background: #4f9e63; }
  .tray .mute, .tray .unmute { background: #c9a13f; }
  .tray .delete { background: #d0564f; color: #fff; }
  .tray .delete.confirm { background: #a8322c; }
  .swipe.failed .tray .delete { outline: 2px solid #fff; outline-offset: -4px; }
  /* The star a starred row carries in front of its title. */
  .star-mark { flex: none; color: #e3b341; font-size: .9em; line-height: 1; }
  @media (prefers-reduced-motion: reduce) { .swipe > .row { transition: none !important; } }
`;

// The tray's Star button, the same on both lists.
export function starButtonHtml(starred, attrs) {
  return starred
    ? `<button type="button" class="unstar" ${attrs('unstar')} title="Take it off the top of the list">Unstar</button>`
    : `<button type="button" class="star" ${attrs('star')} title="Keep it at the top of the list">&#9733; Star</button>`;
}

// The mark in front of a starred row's title.
export const STAR_MARK = '<span class="star-mark" role="img" aria-label="Starred" title="Starred">&#9733;</span>';

export class SwipeRows {
  // `host`: the component element (presses outside it close an open tray).
  // `onQuick(id, action, el)`: a committed swipe; action is the row's
  // data-quick for a swipe right, its data-quick-left for a swipe left (on a
  // row that opts into one). `onTap(id, el, event)`: a plain tap on a row (optional — a
  // link row navigates by itself). `isBusy()`: a write is under way, so no
  // new gesture starts.
  constructor(host, { onQuick, onTap = null, isBusy = () => false } = {}) {
    this._host = host;
    this._onQuick = onQuick;
    this._onTap = onTap;
    this._isBusy = isBusy;
    this._root = null;
    this._drag = null;
    this._openId = null;
    this._confirmId = null;
    this._suppressUntil = 0;
    this._offOutside = null;
  }

  // A finger is down on a row: a re-render now would drop the row it drags.
  get dragging() { return !!this._drag; }
  get openId() { return this._openId; }

  // Forget the open tray and an armed Delete (a scope switch, a new list).
  reset() {
    this._openId = null;
    this._confirmId = null;
  }

  disconnect() {
    if (this._offOutside) this._offOutside();
    this._offOutside = null;
  }

  // Wire every row under `root`; call after each render of the rows.
  wire(root) {
    this._root = root;
    const rows = [...root.querySelectorAll('.swipe')];
    rows.forEach((el) => {
      const row = el.querySelector('.row');
      if (!row) return;
      row.addEventListener('pointerdown', (e) => this._dragStart(e, el, row));
      row.addEventListener('pointermove', (e) => this._dragMove(e));
      row.addEventListener('pointerup', (e) => this._dragEnd(e));
      row.addEventListener('pointercancel', () => this._dragCancel());
      // A swipe ends on the row, and the browser then clicks it: swallow that
      // click, and a tap on an open row closes it rather than opening it.
      row.addEventListener('click', (e) => {
        const swiped = performance.now() < this._suppressUntil;
        if (swiped || this._openId === el.dataset.id) {
          e.preventDefault();
          this._suppressUntil = 0;
          if (!swiped) this.close();
          return;
        }
        if (this._openId) {
          e.preventDefault();
          this.close();
          return;
        }
        if (this._onTap) this._onTap(el.dataset.id, el, e);
      });
      // Long press, right-click, the context-menu key: the tray, for whoever
      // cannot or would rather not swipe. From the keyboard, focus lands on
      // its first button.
      row.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        this._drag = null;
        this.open(el);
        if (!e.pointerType || e.pointerType === 'mouse') {
          const first = el.querySelector('.tray button');
          if (first && e.button !== 2) first.focus();
        }
      });
    });
    // Re-open the tray a refresh rebuilt, without replaying the slide, and
    // keep an armed Delete armed.
    if (this._openId) {
      const el = rows.find((r) => r.dataset.id === this._openId);
      if (el) {
        const del = el.querySelector('.tray .delete');
        if (del && this._confirmId === this._openId) this._arm(del);
        this.open(el, false);
      } else {
        this.reset();
      }
    }
    if (!this._offOutside) {
      this._offOutside = onPressOutside(this._host, () => { if (this._openId) this.close(); });
    }
  }

  rowEl(id) {
    if (!id || !this._root) return null;
    return [...this._root.querySelectorAll('.swipe')].find((el) => el.dataset.id === id) || null;
  }

  _trayWidth(el) {
    const tray = el.querySelector('.tray');
    return tray ? tray.offsetWidth : 0;
  }

  // `left` names what a row moved to the left uncovers: the tray, or — while
  // a row that commits to the left is dragged that way — its quick-left layer.
  slide(el, x, animate = true, left = 'tray') {
    const row = el.querySelector('.row');
    if (!row) return;
    row.style.transition = animate ? 'transform .18s ease-out' : 'none';
    row.style.transform = x ? `translateX(${x}px)` : '';
    // Only the side being uncovered shows, so its colour never bleeds through
    // the other edge while a row slides back.
    el.classList.toggle('show-quick', x > 0);
    el.classList.toggle('show-tray', x < 0 && left === 'tray');
    el.classList.toggle('show-quick-left', x < 0 && left === 'quick');
  }

  open(el, animate = true) {
    if (this._openId && this._openId !== el.dataset.id) this.close();
    this._openId = el.dataset.id;
    this.slide(el, -this._trayWidth(el), animate);
  }

  close() {
    const el = this.rowEl(this._openId);
    this.reset();
    if (el) {
      this.slide(el, 0);
      el.querySelectorAll('.confirm').forEach((b) => this._unarm(b));
    }
  }

  // Delete cannot be undone, so the first tap only arms it: the button says
  // what a second tap will do — "Delete for good?", or the row's own words
  // where its delete does something else as well (`data-confirm`: a mail
  // thread's mails move to the mailbox's Trash) — and anything else disarms
  // it. True when this tap is the second one, which the caller then carries
  // out.
  confirm(id, btn) {
    if (this._confirmId !== id) {
      this._confirmId = id;
      this._arm(btn);
      // The tray grew; keep the row flush with its new edge.
      const el = this.rowEl(id);
      if (el) this.slide(el, -this._trayWidth(el));
      return false;
    }
    this._confirmId = null;
    return true;
  }

  _arm(btn) {
    btn.classList.add('confirm');
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    btn.textContent = (btn.dataset && btn.dataset.confirm) || 'Delete for good?';
  }

  _unarm(btn) {
    btn.classList.remove('confirm');
    if (btn.dataset.label) btn.textContent = btn.dataset.label;
  }

  _dragStart(e, el, row) {
    if (e.button !== 0 || this._isBusy()) return;
    const base = this._openId === el.dataset.id ? -this._trayWidth(el) : 0;
    // A row that commits to the left does so from rest; with its tray open
    // (a long press put it there) the drag moves the tray as on any row.
    const left = el.dataset.quickLeft && !base ? 'quick' : 'tray';
    this._drag = { el, row, id: e.pointerId, x0: e.clientX, y0: e.clientY, base, dx: 0, axis: null, left };
  }

  _dragMove(e) {
    const d = this._drag;
    if (!d || e.pointerId !== d.id) return;
    const dx = e.clientX - d.x0;
    const dy = e.clientY - d.y0;
    if (!d.axis) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      d.axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      if (d.axis === 'y') { this._drag = null; return; }
      if (this._openId && this._openId !== d.el.dataset.id) this.close();
      try { d.row.setPointerCapture(d.id); } catch (_e) { /* already released */ }
    }
    e.preventDefault();
    const width = d.el.offsetWidth;
    const tray = this._trayWidth(d.el);
    // Past the tray's edge the row resists instead of stopping dead, so the
    // limit is felt; the quick side runs to most of the row's width — both
    // quick sides, on a row that commits to the left as well.
    let x = d.base + dx;
    if (d.left === 'quick') {
      if (x < -width * 0.8) x = -width * 0.8;
    } else if (x < -tray) {
      x = -tray + (x + tray) / 4;
    }
    if (x > width * 0.8) x = width * 0.8;
    // Nothing to commit on the right: the row stays where it is on that side.
    if (x > 0 && !d.el.dataset.quick) x = 0;
    d.dx = x;
    this.slide(d.el, x, false, d.left);
  }

  _dragEnd(e) {
    const d = this._drag;
    if (!d || e.pointerId !== d.id) return;
    this._drag = null;
    if (d.axis !== 'x') return;
    // The click a mouse drag ends in must not open the row. A touch pan
    // produces no click, so this lapses rather than eating the next tap.
    this._suppressUntil = performance.now() + 400;
    const width = d.el.offsetWidth;
    const tray = this._trayWidth(d.el);
    const past = Math.min(120, width * 0.35);
    const commit = (x, action) => {
      // Committed: slide the row out and act. The quick action is the one
      // the row's state calls for — out of the list, or back into it.
      this._openId = null;
      this.slide(d.el, x, true, d.left);
      const id = d.el.dataset.id;
      const el = d.el;
      setTimeout(() => this._onQuick(id, action, el), 160);
    };
    if (d.dx > past && d.el.dataset.quick) {
      commit(width, d.el.dataset.quick);
    } else if (d.left === 'quick') {
      // The left side commits like the right one, and never opens the tray.
      if (d.dx < -past) commit(-width, d.el.dataset.quickLeft);
      else this.slide(d.el, 0);
    } else if (d.dx < -tray / 2) {
      this.open(d.el);
    } else {
      if (this._openId === d.el.dataset.id) this._openId = null;
      this.slide(d.el, 0);
    }
  }

  _dragCancel() {
    const d = this._drag;
    this._drag = null;
    if (d && d.axis === 'x') {
      if (this._openId === d.el.dataset.id) this.open(d.el);
      else this.slide(d.el, 0);
    }
  }
}

// ── Undo ────────────────────────────────────────────────────────────────────
// One toast for the whole page, outside every component's shadow root, so a
// list re-rendering under it (as both lists do on every refresh) cannot take
// it away. A newer offer replaces an older one; the older one simply lapses.

let toastHost = null;
let toastTimer = 0;

const TOAST_CSS = `
  .toast { position: fixed; left: 50%; transform: translateX(-50%); z-index: 30;
           bottom: calc(env(safe-area-inset-bottom, 0px) + 84px); max-width: calc(100% - 32px);
           display: flex; align-items: center; gap: 14px; padding: 10px 14px; border-radius: 12px;
           background: var(--card, #151922); color: var(--fg, #e7ebf2); font: 400 .85rem/1.4 system-ui, sans-serif;
           border: 1px solid var(--line, rgba(231, 235, 242, .12)); box-shadow: 0 6px 20px rgba(0, 0, 0, .4); }
  .t { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  button { flex: none; background: none; border: 0; padding: 2px 4px; cursor: pointer; font: inherit;
           color: var(--accent, #6ea8fe); font-weight: 600; }
`;

export function dismissUndo() {
  clearTimeout(toastTimer);
  if (toastHost && toastHost.shadowRoot) toastHost.shadowRoot.innerHTML = '';
}

// Offer the way back from a change for UNDO_MS: `text` says what happened,
// `onUndo` puts it back.
export function offerUndo(text, onUndo) {
  if (typeof document === 'undefined') return;
  if (!toastHost || !toastHost.isConnected) {
    toastHost = document.createElement('div');
    toastHost.setAttribute('data-undo-toast', '');
    toastHost.attachShadow({ mode: 'open' });
    document.body.appendChild(toastHost);
  }
  clearTimeout(toastTimer);
  const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  toastHost.shadowRoot.innerHTML = `<style>${TOAST_CSS}</style>` +
    `<div class="toast" role="status"><span class="t">${esc(text)}</span>` +
    `<button type="button">Undo</button></div>`;
  toastHost.shadowRoot.querySelector('button').addEventListener('click', () => {
    dismissUndo();
    onUndo();
  });
  toastTimer = setTimeout(dismissUndo, UNDO_MS);
}
