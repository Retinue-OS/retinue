// Chats card: the messenger mirror's home screen — every channel conversation
// (Signal / WhatsApp / Telegram, one peer or group each) as one row, ordered by
// last activity, with unread badge, channel mark and last-message preview. Rows
// link to the chat's own page (chat.html), which renders the full mirror beside
// its companion thread. With the `full` attribute (chats.html) the list drops
// the dashboard cap and adds an Active/Archived filter, like the conversations
// page. Archived chats are excluded everywhere else; `muted` only changes the
// badge treatment here (its real meaning — no Web Push, and no un-archive on a
// new inbound message — is the server's).
//
// On the full page each row is swiped, messenger-style, rather than carrying
// buttons that eat a third of a phone's width. Two things one does to a chat
// that is in the way are not the same thing: **Archive** puts it away until it
// speaks again, **Mute** puts it away and keeps it there. Muting archives, so
// both land in the Archived tab, and **Restore** undoes either. That is the
// dashboard-conversation model verbatim, and it is what replaced a sender
// blacklist: not wanting to hear from someone is a chat one mutes, on the chat,
// in front of the user — not a list only Ara can edit.
//
//   Active tab    swipe right  archive at once
//                 swipe left   reveal Archive · Mute
//   Archived tab  swipe right  restore at once
//                 swipe left   reveal Restore · Mute (Unmute) · Delete
//
// **Delete** erases the chat from the whole system — every message and file in
// the ledger, its companion thread — and a later message from the peer starts
// a new chat; it asks for a second tap. A long press, a right-click or the
// context-menu key opens the same shelf without swiping.
//
// The list comes from the gateway's GET /chats (the default `src`, still
// overridable by attribute) — SPARQL over the message ledgers merged with the
// live overlay and the chat state. The response shape is documented in
// webapp/README.md, "Messenger chats"; the reference documents under
// webapp/data/chats* mirror it for tests. The card refreshes on an ambient
// cadence and keeps its last rendered state over a failed fetch (a store blip
// must not blank the list).

import { RetinueCard, esc, fmtAge, isWideFrame, onFrameChange } from './base.js';

const LIST_URL = '/chats';
// Rows shown on the dashboard card before "All chats →" takes over — the same
// cap logic as the conversations card: only the phone layout, where each row
// lengthens the page, caps the list.
const MAX_CARD_CHATS = 5;
// Ambient refresh: the card carries summaries, not the open thread — a gentler
// cadence than the conversations card's 4s poll is enough (the server caches
// the SPARQL skeleton between polls anyway).
const REFRESH_MS = 15000;

// Channel marks: no brand assets in the shell, so a lettered dot in the
// channel's recognisable colour does the telling.
export const CHANNELS = {
  signal: { label: 'Signal', mark: 'S', color: '#3a76f0' },
  whatsapp: { label: 'WhatsApp', mark: 'W', color: '#25d366' },
  telegram: { label: 'Telegram', mark: 'T', color: '#2aabee' },
  sms: { label: 'SMS', mark: 'M', color: '#8e7cc3' },
};

export function channelMarkHtml(channel) {
  const c = CHANNELS[channel] || { label: channel, mark: '?', color: 'var(--muted, #8b93a3)' };
  return `<span class="ch" style="background:${c.color}" title="${esc(c.label)}" ` +
    `aria-label="${esc(c.label)}">${esc(c.mark)}</span>`;
}

// Deterministic avatar/sender colour from any stable key (chat id, sender key):
// a small palette that reads on the dark shell, same hue for the same person on
// every render and every page.
const AVATAR_COLORS = ['#b0713f', '#3f8fb0', '#7d6cc9', '#4f9e63', '#b04f77', '#8a9a3d', '#c07840'];

export function colorFor(key) {
  let h = 0;
  const s = String(key || '');
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

export function initials(name) {
  const words = String(name || '?').trim().split(/\s+/).filter(Boolean);
  const first = (w) => {
    // First base character, robust for astral-plane characters (emoji names).
    const cp = w.codePointAt(0);
    return cp ? String.fromCodePoint(cp).toUpperCase() : '';
  };
  if (!words.length) return '?';
  if (words.length === 1) return first(words[0]);
  return first(words[0]) + first(words[words.length - 1]);
}

// Avatar disc (deterministic colour, initials) with the channel mark docked to
// its corner — one glance answers both "who" and "over which channel".
// The avatar's colour identifies the PEER, not the chat: it is keyed on the
// channel and chat key, so the same person keeps one colour across every
// account that talks to them — including the unattributed history beside an
// account-named chat. Keying it on the chat id instead would paint one person
// in as many colours as there are chats with them, in exactly the list where
// telling those chats apart is already the difficulty. The key rather than the
// name, because a roster update renames a contact and must not repaint them.
export function avatarHtml(chat) {
  const peer = `${chat.channel || ''}\u0000${chat.key || chat.id || ''}`;
  return `<span class="av" style="background:${colorFor(peer)}" aria-hidden="true">` +
    `${esc(initials(chat.name))}${channelMarkHtml(chat.channel)}</span>`;
}

// The one-line preview under the chat name, messenger-home style: who said the
// last thing, then what. Outbound gets its author ("You", the agent's name,
// "You (phone)" for an own-device echo); group inbound gets the sender's first
// name. An image message shows a camera mark before any caption.
export function previewHtml(chat) {
  const last = chat.last || {};
  let who = '';
  if (last.direction === 'out') {
    who = last.author === 'agent' ? 'Ara'
      : last.author === 'device' ? 'You (phone)' : 'You';
  } else if (chat.group && last.sender_name) {
    who = String(last.sender_name).split(/\s+/)[0];
  }
  const img = last.kind === 'image' ? '<span class="pv-img" aria-label="Image">&#128247;</span> ' : '';
  const text = last.text || (last.kind === 'image' ? 'Photo' : '');
  return (who ? `<span class="pv-who">${esc(who)}:</span> ` : '') + img + esc(text);
}

class RetinueChats extends RetinueCard {
  connectedCallback() {
    this._full = this.hasAttribute('full');
    this._scope = 'active';  // full-mode filter: active | archived
    // Bumped by a flag write. A refresh that began before one is stale by the
    // time it answers — it carries the pre-hide list — and rendering it would
    // put the row back and flip the button to the wrong inverse until the next
    // poll. The epoch is how such an answer is recognised and dropped.
    this._epoch = 0;
    this._openId = null;     // the row whose action shelf is open, if any
    this._confirmId = null;  // the row whose Delete awaits its second tap
    // Crossing the layout breakpoint changes how many rows fit (cap vs all).
    this._offFrame = onFrameChange(() => {
      if (this._data) this.renderState({ state: 'ok', data: this._data });
    });
    super.connectedCallback();
    this._timer = setInterval(() => this.load(), REFRESH_MS);
  }

  disconnectedCallback() {
    if (this._offFrame) this._offFrame();
    this._offFrame = null;
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
  }

  get dataUrl() { return this.getAttribute('src') || LIST_URL; }

  // Unlike the one-shot base loader, a refreshing card must not blank itself
  // over one failed fetch: keep the last rendered list and let the next tick
  // reconcile. Only a failure with nothing rendered yet shows the offline
  // state.
  async load() {
    const epoch = this._epoch;
    try {
      const res = await fetch(this.dataUrl, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      // A hide or unhide landed while this was in flight: this answer predates
      // it and would undo it on screen. The write already re-rendered, and the
      // next tick fetches the list as it now is.
      if (epoch !== this._epoch) return;
      // Re-render only when the list actually changed — a rebuild would reset
      // the region's scroll and the filter wiring for nothing.
      const sig = JSON.stringify(data.chats || []);
      if (sig === this._sig) { this._data = data; return; }
      // A rebuild under a finger mid-swipe would drop the row it is dragging;
      // the next tick picks the change up.
      if (this._drag) return;
      this._sig = sig;
      this.renderState({ state: 'ok', data });
    } catch (_err) {
      if (!this._data) this.renderState({ state: 'offline' });
    }
  }

  // RetinueCard renders static content; the full page's scope filter and its
  // per-row shelf buttons are the interactive parts, wired after each render.
  renderState(s) {
    super.renderState(s);
    this.shadowRoot.querySelectorAll('[data-scope]').forEach((el) =>
      el.addEventListener('click', () => {
        const scope = el.getAttribute('data-scope');
        if (scope === this._scope) return;
        this._scope = scope;
        this._openId = null;
        this._confirmId = null;
        if (this._data) this.renderState({ state: 'ok', data: this._data });
      }));
    this.shadowRoot.querySelectorAll('[data-flag]').forEach((el) =>
      el.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const id = el.getAttribute('data-flag');
        const action = el.getAttribute('data-set');
        if (action === 'delete') this._confirmDelete(id, el);
        else this._setFlags(id, action);
      }));
    if (this._full) this._wireSwipe();
  }

  // ── Swipe ────────────────────────────────────────────────────────────────
  // Pointer events, so a finger, a pen and a mouse drag all work. The row
  // declares `touch-action: pan-y`: the browser keeps vertical scrolling and
  // hands horizontal movement to us. A gesture is claimed as a swipe only
  // once it is clearly horizontal, so a scroll that starts on a row is still
  // a scroll.
  _wireSwipe() {
    const root = this.shadowRoot;
    const rows = root.querySelectorAll('li.swipe');
    rows.forEach((li) => {
      const row = li.querySelector('.row');
      if (!row) return;
      row.addEventListener('pointerdown', (e) => this._dragStart(e, li, row));
      row.addEventListener('pointermove', (e) => this._dragMove(e));
      row.addEventListener('pointerup', (e) => this._dragEnd(e));
      row.addEventListener('pointercancel', () => this._dragCancel());
      // A swipe ends on the row, and the browser then clicks it: swallow that
      // click, and a tap on an open row closes it rather than navigating.
      row.addEventListener('click', (e) => {
        const swiped = performance.now() < (this._suppressUntil || 0);
        if (swiped || this._openId === li.dataset.id) {
          e.preventDefault();
          this._suppressUntil = 0;
          if (!swiped) this._close();
        } else if (this._openId) {
          e.preventDefault();
          this._close();
        }
      });
      // Long press, right-click, the context-menu key: the shelf, for
      // whoever cannot or would rather not swipe. From the keyboard, focus
      // lands on its first button.
      row.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        this._drag = null;
        this._open(li);
        if (!e.pointerType || e.pointerType === 'mouse') {
          const first = li.querySelector('.shelf button');
          if (first && e.button !== 2) first.focus();
        }
      });
    });
    // Re-open the shelf a refresh rebuilt, without replaying the slide, and
    // keep an armed Delete armed.
    if (this._openId) {
      const li = [...rows].find((el) => el.dataset.id === this._openId);
      if (li) {
        const del = li.querySelector('[data-set="delete"]');
        if (del && this._confirmId === this._openId) this._arm(del);
        this._open(li, false);
      } else {
        this._openId = null;
        this._confirmId = null;
      }
    }
    if (!this._outsideWired) {
      this._outsideWired = true;
      document.addEventListener('pointerdown', (e) => {
        if (this._openId && !e.composedPath().includes(this)) this._close();
      });
    }
  }

  _shelfWidth(li) {
    const shelf = li.querySelector('.shelf');
    return shelf ? shelf.offsetWidth : 0;
  }

  _slide(li, x, animate = true) {
    const row = li.querySelector('.row');
    row.style.transition = animate ? 'transform .18s ease-out' : 'none';
    row.style.transform = x ? `translateX(${x}px)` : '';
    // Only the side being uncovered shows, so its colour never bleeds through
    // the other edge while a row slides back.
    li.classList.toggle('show-quick', x > 0);
    li.classList.toggle('show-shelf', x < 0);
  }

  _open(li, animate = true) {
    if (this._openId && this._openId !== li.dataset.id) this._close();
    this._openId = li.dataset.id;
    this._slide(li, -this._shelfWidth(li), animate);
  }

  _close() {
    const li = this._rowEl(this._openId);
    this._openId = null;
    this._confirmId = null;
    if (li) {
      this._slide(li, 0);
      li.querySelectorAll('.confirm').forEach((b) => this._unconfirm(b));
    }
  }

  _rowEl(id) {
    if (!id) return null;
    return [...this.shadowRoot.querySelectorAll('li.swipe')].find((el) => el.dataset.id === id) || null;
  }

  _dragStart(e, li, row) {
    if (e.button !== 0 || this._flagging) return;
    const base = this._openId === li.dataset.id ? -this._shelfWidth(li) : 0;
    this._drag = { li, row, id: e.pointerId, x0: e.clientX, y0: e.clientY, base, dx: 0, axis: null };
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
      if (this._openId && this._openId !== d.li.dataset.id) this._close();
      try { d.row.setPointerCapture(d.id); } catch (_e) { /* already released */ }
    }
    e.preventDefault();
    const width = d.li.offsetWidth;
    const shelf = this._shelfWidth(d.li);
    // Past the shelf's edge the row resists instead of stopping dead, so the
    // limit is felt; the quick side runs to most of the row's width.
    let x = d.base + dx;
    if (x < -shelf) x = -shelf + (x + shelf) / 4;
    if (x > width * 0.8) x = width * 0.8;
    d.dx = x;
    this._slide(d.li, x, false);
  }

  _dragEnd(e) {
    const d = this._drag;
    if (!d || e.pointerId !== d.id) return;
    this._drag = null;
    if (d.axis !== 'x') return;
    // The click a mouse drag ends in must not open the chat. A touch pan
    // produces no click, so this lapses rather than eating the next tap.
    this._suppressUntil = performance.now() + 400;
    const width = d.li.offsetWidth;
    const shelf = this._shelfWidth(d.li);
    if (d.dx > Math.min(120, width * 0.35)) {
      // Committed: slide the row out and act. The quick action is the one
      // the tab is about — out of the list, or back into it.
      this._openId = null;
      this._slide(d.li, width);
      const action = d.li.dataset.quick;
      setTimeout(() => this._setFlags(d.li.dataset.id, action, d.li), 160);
    } else if (d.dx < -shelf / 2) {
      this._open(d.li);
    } else {
      if (this._openId === d.li.dataset.id) this._openId = null;
      this._slide(d.li, 0);
    }
  }

  _dragCancel() {
    const d = this._drag;
    this._drag = null;
    if (d && d.axis === 'x') {
      if (this._openId === d.li.dataset.id) this._open(d.li);
      else this._slide(d.li, 0);
    }
  }

  // Delete cannot be undone, so the first tap only arms it: the button says
  // what a second tap will do, and anything else disarms it.
  _confirmDelete(id, btn) {
    if (this._confirmId !== id) {
      this._confirmId = id;
      this._arm(btn);
      // The shelf grew; keep the row flush with its new edge.
      const li = this._rowEl(id);
      if (li) this._slide(li, -this._shelfWidth(li));
      return;
    }
    this._confirmId = null;
    this._deleteChat(id);
  }

  _arm(btn) {
    btn.classList.add('confirm');
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    btn.textContent = 'Delete for good?';
  }

  _unconfirm(btn) {
    btn.classList.remove('confirm');
    if (btn.dataset.label) btn.textContent = btn.dataset.label;
  }

  async _deleteChat(id) {
    if (!id || this._flagging) return;
    this._flagging = true;
    const busy = this._rowEl(id);
    if (busy) busy.classList.add('busy');
    try {
      const res = await fetch(`/chats/${encodeURIComponent(id)}/delete`, { method: 'POST' });
      if (!res.ok) throw new Error(res.status === 503 ? 'unconfigured' : String(res.status));
      if (this._data && Array.isArray(this._data.chats)) {
        this._data.chats = this._data.chats.filter((c) => c.id !== id);
      }
      this._openId = null;
      this._sig = '';
      this._epoch += 1;
      if (this._data) this.renderState({ state: 'ok', data: this._data });
    } catch (err) {
      // The chat stays whole enough to retry (the gateway answers 502 and
      // touches none of its own state when the messages cannot all go). A 503
      // is a deployment without the erase capability: retrying will not help,
      // and the button says so.
      const li = this._rowEl(id);
      if (li) {
        li.classList.remove('busy');
        li.classList.add('failed');
        const btn = li.querySelector('[data-set="delete"]');
        if (btn) {
          this._unconfirm(btn);
          const off = err && err.message === 'unconfigured';
          btn.textContent = off ? 'Delete not set up' : 'Retry delete';
          if (off) btn.title = 'Chat deletion needs CHAT_ERASE_TOKEN on retinue and the messenger gateways';
        }
      }
    } finally {
      this._flagging = false;
    }
  }

  // Three actions over the same two flags, exactly as a conversation has:
  //
  //   Archive  out of the list, until the chat speaks again
  //   Mute     out of the list, and the next message does not bring it back
  //   Restore  back in the list, and speaking again
  //
  // Muting archives — the server's rule, not this button's — so both ways out
  // land in the same place and either is undone the same way. Whether a chat
  // also reaches the Herald is the triage policy's business and not this
  // button's: the two are independent, so a chat can be a news source and
  // still sit in the list.
  async _setFlags(id, action, li = null) {
    const FLAGS = {
      archive: { archived: true },
      mute: { muted: true },
      // Stays archived — it only lets the next message bring the chat back.
      unmute: { muted: false },
      restore: { archived: false, muted: false },
    };
    const flags = FLAGS[action];
    if (!id || !flags || this._flagging) return;
    this._flagging = true;
    try {
      const res = await fetch(`/chats/${encodeURIComponent(id)}/flags`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(flags),
      });
      if (!res.ok) throw new Error(String(res.status));
      // Take the flags back from the answer rather than assuming them: muting
      // archives server-side, and this way the rule lives in one place.
      const doc = await res.json();
      const chat = (this._data && this._data.chats || []).find((c) => c.id === id);
      if (chat) { chat.archived = !!doc.archived; chat.muted = !!doc.muted; }
      this._openId = null;
      this._confirmId = null;
      this._sig = '';
      // Any refresh already on the wire answers from before this write.
      this._epoch += 1;
      if (this._data) this.renderState({ state: 'ok', data: this._data });
    } catch (_err) {
      // Left as it was; a row swiped out slides back, and the next swipe or
      // tap can retry.
      if (li) this._slide(li, 0);
    } finally {
      this._flagging = false;
    }
  }

  css() {
    return `
      /* Chrome-less on phones, edge to edge under the navigation like the
         threads and projects pages; a framed card again on wide screens. */
      .card { background: transparent; padding: 2px; }
      @media (min-width: 700px) {
        .card { background: var(--card, #151922); border: 1px solid var(--line, rgba(231, 235, 242, .08));
                padding: 14px 16px; }
      }
      ul.list { gap: 4px; }
      li { margin: 0; }
      /* A swipeable row: the link slides over two layers underneath — the
         quick action on the left (uncovered by a swipe right) and the shelf
         of buttons on the right (uncovered by a swipe left). The row needs
         an opaque background of its own to hide them while it rests. */
      li.swipe { position: relative; overflow: hidden; border-radius: 12px; }
      li.swipe .row { position: relative; z-index: 1; background: var(--card, #151922);
                      touch-action: pan-y; user-select: none; -webkit-user-select: none;
                      -webkit-touch-callout: none; }
      li.swipe .row:hover { background: var(--card-2, #1c2230); }
      li.swipe.busy .row { opacity: .5; }
      /* visibility, not just the row on top: a hidden shelf's buttons also
         leave the tab order, and an open one's are all reachable by Tab. */
      .quick, .shelf { position: absolute; top: 0; bottom: 0; display: flex;
                       align-items: stretch; visibility: hidden; }
      li.show-quick .quick, li.show-shelf .shelf { visibility: visible; }
      .quick { left: 0; right: 0; padding-left: 18px; align-items: center;
               font-size: .8rem; font-weight: 600; color: #0b0d12; }
      .quick.archive { background: var(--accent, #6ea8fe); }
      .quick.restore { background: #4f9e63; }
      .shelf { right: 0; }
      .shelf button { border: 0; margin: 0; padding: 0 14px; min-width: 68px; cursor: pointer;
                      font: inherit; font-size: .76rem; font-weight: 600; color: #0b0d12;
                      white-space: nowrap; -webkit-tap-highlight-color: transparent; }
      .shelf button:focus-visible { outline: 2px solid var(--fg, #e7ebf2); outline-offset: -3px; }
      .shelf .archive { background: var(--accent, #6ea8fe); }
      .shelf .restore { background: #4f9e63; }
      .shelf .mute, .shelf .unmute { background: #c9a13f; }
      .shelf .delete { background: #d0564f; color: #fff; }
      .shelf .delete.confirm { background: #a8322c; }
      li.failed .shelf .delete { outline: 2px solid #fff; outline-offset: -4px; }
      .row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto;
             grid-template-rows: auto auto; align-items: center; column-gap: 10px; row-gap: 1px;
             padding: 8px 10px; border-radius: 12px; text-decoration: none; color: var(--fg, #e7ebf2);
             background: transparent; -webkit-tap-highlight-color: transparent; }
      .row:hover { background: var(--card-2, #1c2230); }
      .av { grid-row: 1 / 3; position: relative; width: 40px; height: 40px; border-radius: 50%;
            display: inline-flex; align-items: center; justify-content: center;
            color: rgba(255, 255, 255, .92); font-size: .85rem; font-weight: 700; }
      .ch { position: absolute; right: -3px; bottom: -3px; width: 16px; height: 16px;
            border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
            font-size: .58rem; font-weight: 800; color: #fff;
            border: 2px solid var(--bg, #0b0d12); box-sizing: content-box; }
      @media (min-width: 700px) { .ch { border-color: var(--card, #151922); } }
      .name { font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; }
      /* Which account this row is, shown only when a name repeats within a
         channel. Quiet and inline: it disambiguates, it is not a second name. */
      .name .acct { margin-left: 6px; font-weight: 400; font-size: .72rem;
                    color: var(--muted, #8b93a3); }
      .when { grid-column: 3; color: var(--muted, #8b93a3); font-size: .72rem; white-space: nowrap; }
      .row.has-unread .when { color: var(--accent, #6ea8fe); }
      .prev { grid-column: 2; color: var(--muted, #8b93a3); font-size: .8rem; line-height: 1.35;
              min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .prev .pv-who { color: var(--fg, #e7ebf2); opacity: .75; }
      .prev .pv-draft { color: var(--accent, #6ea8fe); font-style: italic; }
      .unread { grid-column: 3; justify-self: end; min-width: 18px; height: 18px; padding: 0 5px;
                border-radius: 9px; background: var(--accent, #6ea8fe); color: #0b0d12;
                font-size: .68rem; font-weight: 700; display: inline-flex;
                align-items: center; justify-content: center; }
      .unread.muted-chat { background: var(--card-2, #1c2230); color: var(--muted, #8b93a3); }
      .mute-mark { grid-column: 3; justify-self: end; color: var(--muted, #8b93a3);
                   font-size: .68rem; }
      .foot { display: flex; flex-direction: column; gap: 10px; padding-top: 12px; }
      .all-link { color: var(--accent, #6ea8fe); text-decoration: none; font-size: .85rem;
                  text-align: center; padding: 2px; }
      .all-link:hover { text-decoration: underline; }
      /* Active/Archived switch — full page only; same look as the
         conversations page's filter. */
      .filter { display: flex; background: var(--card-2, #1c2230); border-radius: 12px;
                padding: 3px; margin-bottom: 10px; }
      .filter-tab { flex: 1; background: transparent; border: 0; border-radius: 9px; padding: 7px;
                    color: var(--muted, #8b93a3); cursor: pointer; }
      .filter-tab.on { background: var(--accent, #6ea8fe); color: #0b0d12; font-weight: 600; }
    `;
  }

  // The Active/Archived switch shown on the full page. No pinning and no
  // unread-only view — deliberately deferred (see the README contract notes).
  _filterHtml() {
    if (!this._full) return '';
    const tab = (scope, label) =>
      `<button class="filter-tab${this._scope === scope ? ' on' : ''}" ` +
      `data-scope="${scope}">${label}</button>`;
    return `<div class="filter">${tab('active', 'Active')}${tab('archived', 'Archived')}</div>`;
  }

  body(d) {
    this._data = d;
    const all = Array.isArray(d.chats) ? d.chats.slice() : [];
    // The API contract returns the list ordered by last activity; keep that
    // guarantee client-side too, so a hand-edited fixture cannot scramble it.
    all.sort((a, b) => String((b.last || {}).ts || '').localeCompare(String((a.last || {}).ts || '')));
    // Archived chats leave the card and the Active list; the full page's
    // Archived filter is where they remain reachable — and where Restore puts
    // them back.
    const archived = this._full && this._scope === 'archived';
    const chats = all.filter((c) => !!c.archived === archived);
    if (!chats.length) {
      const msg = archived ? 'No archived chats.' : 'No chats yet.';
      return `${this._filterHtml()}<p class="muted">${msg}</p>${this._footHtml()}`;
    }
    const shown = (this._full || isWideFrame()) ? chats : chats.slice(0, MAX_CARD_CHATS);
    // Two accounts of one channel talking to the same peer are two chats with
    // the same name (and so is a peer's unattributed history beside its
    // account-named chat). The name alone cannot tell them apart, so where —
    // and only where — a name repeats within a channel, each row also says
    // which account it is. Computed over the rows actually shown, so an
    // unambiguous list carries no such label at all.
    const nameCount = {};
    for (const c of shown) {
      const k = `${c.channel}\u0000${c.name}`;
      nameCount[k] = (nameCount[k] || 0) + 1;
    }
    const rows = shown.map((c) => {
      const badge = c.unread
        ? `<span class="unread${c.muted ? ' muted-chat' : ''}">${c.unread}</span>`
        : (c.muted ? '<span class="mute-mark" title="Muted" aria-label="Muted">&#128277;</span>' : '');
      // A staged draft outranks the last message in the preview — it is what
      // this chat is waiting on.
      const draft = c.draft && c.draft.text;
      const prev = draft
        ? `<span class="pv-draft">${c.draft.author === 'agent'
          ? `Draft by ${esc(c.draft.agent || 'Ara')}` : 'Draft'}:</span> ${esc(c.draft.text)}`
        : previewHtml(c);
      // The actions sit UNDER the row, never inside it: a row is a link, and
      // a button within a link is not a button. Only on the full page — the
      // dashboard card is a glance, not a place to curate.
      const act = (label, set, title) =>
        `<button type="button" class="${set}" data-flag="${esc(c.id)}" data-set="${set}" ` +
        `title="${title}">${label}</button>`;
      const shelf = archived
        ? act('Restore', 'restore', 'Put this chat back in the list') +
          (c.muted
            ? act('Unmute', 'unmute', 'Stay archived, but come back when it speaks')
            : act('Mute', 'mute', 'Keep it out of the list, even when it speaks')) +
          act('Delete', 'delete',
              'Erase this chat and all its messages from the system')
        : act('Archive', 'archive', 'Out of the list until the next message') +
          act('Mute', 'mute',
              'Out of the list, and the next message does not bring it back');
      const quick = archived ? 'restore' : 'archive';
      const under = !this._full ? ''
        : `<div class="quick ${quick}" aria-hidden="true">${archived ? 'Restore' : 'Archive'}</div>` +
          `<div class="shelf">${shelf}</div>`;
      return (this._full
        ? `<li class="swipe" data-id="${esc(c.id)}" data-quick="${quick}">`
        : '<li>') + under +
        `<a class="row${c.unread ? ' has-unread' : ''}"` +
        (this._full ? ' draggable="false" ' : ' ') +
        `href="/chat.html?id=${encodeURIComponent(c.id)}">` +
        `${avatarHtml(c)}` +
        `<span class="name">${esc(c.name)}` +
        (nameCount[`${c.channel}\u0000${c.name}`] > 1
          ? `<span class="acct" title="${c.account
              ? `On the account ${esc(c.account)}`
              : 'Older messages, from before the account was recorded'}">${
              c.account ? esc(c.account) : 'earlier'}</span>`
          : '') +
        `</span>` +
        `<span class="when">${esc(fmtAge((c.last || {}).ts))}</span>` +
        `<span class="prev">${prev}</span>` +
        badge +
        '</a></li>';
    }).join('');
    return `${this._filterHtml()}<ul class="list">${rows}</ul>${this._footHtml()}`;
  }

  // The card leads deeper (the full list). The full page's way back out is
  // the navigation row at the top of chats.html (components/nav.js), as on
  // every list page — not a link after the last chat, which a long list put
  // out of reach until it was scrolled to its end.
  _footHtml() {
    if (this._full) return '';
    return '<div class="foot"><a class="all-link" href="/chats.html">All chats &#8594;</a></div>';
  }
}

customElements.define('retinue-chats', RetinueChats);
