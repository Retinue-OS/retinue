// Chats card: the messenger mirror's home screen — every channel conversation
// (one peer or group each) as one row, ordered by last activity, with unread
// badge, channel mark and last-message preview. Rows link to the chat's own
// page (chat.html), which renders the full mirror beside its companion thread.
// With the `full` attribute (chats.html) the list drops the dashboard cap and
// splits into three tabs — Unread · Read · Archived — by the server's
// `list_group`. Archived chats are excluded everywhere else; `muted` only
// changes the badge treatment here (its real meaning — no Web Push, and no
// un-archive on a new inbound message — is the server's).
//
// **Unread** is a chat whose latest inbound message has been neither replied
// to nor marked read; **Read** is one where it has. Opening a chat reads
// nothing: a chat leaves Unread by a reply (from anywhere) or by Mark as read —
// a swipe here, or the button under its latest message on the chat page — and
// a new inbound message brings it back. The server says which group a chat is
// in (`list_group`); a payload without it is grouped the same way from
// `archived` and the unread count. The unread badge stays on the row: the tab
// says whether a chat wants attention, the badge how much has come in.
//
// On the full page each row is swiped, messenger-style, rather than carrying
// buttons that eat a third of a phone's width — the gesture the threads page
// shares (components/swipe-rows.js). Two things one does to a chat that is in
// the way are not the same thing: **Archive** puts it away until it speaks
// again, **Mute** puts it away and keeps it there. Muting archives, so both
// land in the Archived tab, and **Restore** undoes either. That is the
// dashboard-conversation model verbatim, and it is what replaced a sender
// blacklist: not wanting to hear from someone is a chat one mutes, on the chat,
// in front of the user — not a list only Ara can edit.
//
//   Unread tab    swipe right  mark read at once
//                 swipe left   archive at once
//   Read tab      swipe right  nothing (there is nothing to mark)
//                 swipe left   archive at once
//   Archived tab  swipe right  restore at once
//                 swipe left   reveal Star · Restore · Mute (Unmute) · Delete
//
// On Unread and Read the tray — Star · (Mark read) · Archive · Mute — opens by
// a long press, a right-click or the context-menu key; on every tab that is
// also how the keyboard reaches what a swipe does. **Star** keeps a chat at
// the top of whichever list it is in, and does nothing else. A swipe or tray
// action that takes a row out of view offers Undo for a few seconds — Mark
// read's puts the previous read mark back. **Delete** erases the chat from the
// whole system — every message and file in the ledger, its companion thread —
// and a later message from the peer starts a new chat; it asks for a second
// tap, and cannot be undone.
//
// The list comes from the gateway's GET /chats (the default `src`, still
// overridable by attribute) — SPARQL over the message ledgers merged with the
// live overlay and the chat state. The response shape is documented in
// webapp/README.md, "Messenger chats"; the reference documents under
// webapp/data/chats* mirror it for tests. The card refreshes on an ambient
// cadence and keeps its last rendered state over a failed fetch (a store blip
// must not blank the list).

import { RetinueCard, esc, fmtAge, isWideFrame, onFrameChange, starredFirst } from './base.js';
import { shelf } from './shelf-store.js';
import { SwipeRows, SWIPE_CSS, STAR_MARK, offerUndo, starButtonHtml } from './swipe-rows.js';

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

// The three lists of the full page, in tab order.
export const LIST_GROUPS = ['unread', 'read', 'archived'];

// Which list a chat belongs in by its own flags and count: archived first
// (an archived chat with unread messages is still archived — muted, or not
// yet spoken to since), then unread or read by the count. The same rule the
// server states as `list_group`; used where the payload predates that field,
// and after a write from here changed what it was computed from.
export function deriveListGroup(chat) {
  const c = chat || {};
  if (c.archived) return 'archived';
  return Number(c.unread) > 0 ? 'unread' : 'read';
}

// The list a chat is shown in: the server's word where it gave one.
export function listGroup(chat) {
  const g = chat && chat.list_group;
  return LIST_GROUPS.includes(g) ? g : deriveListGroup(chat);
}

class RetinueChats extends RetinueCard {
  connectedCallback() {
    this._full = this.hasAttribute('full');
    // The full page's tab: unread | read | archived. Held on the element, as
    // the filter it replaced was — a refresh, a write or a breakpoint flip
    // keeps it; a fresh page starts on Unread, where what wants attention is.
    this._scope = 'unread';
    // A write from here changed a chat's group since the last list was
    // fetched: the payload's own `counts` describe the list before it, so the
    // tabs count the rows instead until the next fetch.
    this._dirty = false;
    // Bumped by a flag write. A refresh that began before one is stale by the
    // time it answers — it carries the pre-hide list — and rendering it would
    // put the row back and flip the button to the wrong inverse until the next
    // poll. The epoch is how such an answer is recognised and dropped.
    this._epoch = 0;
    // The full page's rows swipe (swipe-rows.js): a committed swipe is the
    // row's quick action for that side — Mark read or Restore to the right,
    // Archive to the left.
    this._swipe = new SwipeRows(this, {
      onQuick: (id, action, el) => (action === 'read'
        ? this._markRead(id, el) : this._setFlags(id, action, el)),
      isBusy: () => !!this._flagging,
    });
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
    this._swipe.disconnect();
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
      const sig = JSON.stringify([data.chats || [], data.counts || null]);
      if (sig === this._sig) { this._data = data; this._dirty = false; return; }
      // A rebuild under a finger mid-swipe would drop the row it is dragging;
      // the next tick picks the change up.
      if (this._swipe.dragging) return;
      this._sig = sig;
      this._dirty = false;
      this.renderState({ state: 'ok', data });
    } catch (_err) {
      if (!this._data) this.renderState({ state: 'offline' });
    }
  }

  // RetinueCard renders static content; the full page's scope filter and its
  // per-row tray buttons are the interactive parts, wired after each render.
  renderState(s) {
    super.renderState(s);
    this.shadowRoot.querySelectorAll('[data-scope]').forEach((el) => {
      el.addEventListener('click', () => this._setScope(el.getAttribute('data-scope')));
      // A tablist moves with the arrow keys (and Home/End), focus following
      // the selection — the one tab stop the row has (roving tabindex).
      el.addEventListener('keydown', (e) => {
        const at = LIST_GROUPS.indexOf(el.getAttribute('data-scope'));
        const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: LIST_GROUPS.length - 1 }[e.key];
        if (to === undefined) return;
        e.preventDefault();
        const scope = LIST_GROUPS[(to + LIST_GROUPS.length) % LIST_GROUPS.length];
        this._setScope(scope);
        const tab = this.shadowRoot.querySelector(`[data-scope="${scope}"]`);
        if (tab) tab.focus();
      });
    });
    this.shadowRoot.querySelectorAll('[data-flag]').forEach((el) =>
      el.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const id = el.getAttribute('data-flag');
        const action = el.getAttribute('data-set');
        if (action === 'delete') this._confirmDelete(id, el);
        else if (action === 'read') this._markRead(id);
        else this._setFlags(id, action);
      }));
    if (this._full) this._swipe.wire(this.shadowRoot);
  }

  // Switch the full page's tab; the list is already here, so no fetch.
  _setScope(scope) {
    if (!LIST_GROUPS.includes(scope) || scope === this._scope) return;
    this._scope = scope;
    this._swipe.reset();
    if (this._data) this.renderState({ state: 'ok', data: this._data });
  }

  // Delete cannot be undone, so the first tap only arms it (swipe-rows.js);
  // the second erases.
  _confirmDelete(id, btn) {
    if (this._swipe.confirm(id, btn)) this._deleteChat(id);
  }

  async _deleteChat(id) {
    if (!id || this._flagging) return;
    this._flagging = true;
    const busy = this._swipe.rowEl(id);
    if (busy) busy.classList.add('busy');
    try {
      const res = await fetch(`/chats/${encodeURIComponent(id)}/delete`, { method: 'POST' });
      if (!res.ok) throw new Error(res.status === 503 ? 'unconfigured' : String(res.status));
      if (this._data && Array.isArray(this._data.chats)) {
        this._data.chats = this._data.chats.filter((c) => c.id !== id);
      }
      this._swipe.reset();
      this._sig = '';
      this._epoch += 1;
      if (this._data) this.renderState({ state: 'ok', data: this._data });
    } catch (err) {
      // The chat stays whole enough to retry (the gateway answers 502 and
      // touches none of its own state when the messages cannot all go). A 503
      // is a deployment without the erase capability: retrying will not help,
      // and the button says so.
      const li = this._swipe.rowEl(id);
      if (li) {
        li.classList.remove('busy');
        li.classList.add('failed');
        const btn = li.querySelector('[data-set="delete"]');
        if (btn) {
          btn.classList.remove('confirm');
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
  // and the star beside them, which moves the chat to the top of its list
  // and touches nothing else. `action` may also be a flags object: Undo
  // writes back exactly the flags the row had.
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
      star: { starred: true },
      unstar: { starred: false },
    };
    const flags = typeof action === 'string' ? FLAGS[action] : action;
    if (!id || !flags || this._flagging) return;
    const chat0 = (this._data && this._data.chats || []).find((c) => c.id === id);
    const before = chat0 ? { archived: !!chat0.archived, muted: !!chat0.muted } : null;
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
      if (chat) {
        chat.archived = !!doc.archived; chat.muted = !!doc.muted; chat.starred = !!doc.starred;
        // Archive and Restore move the row between tabs; the server's group
        // was computed before them, so the flags just written decide it.
        chat.list_group = deriveListGroup(chat);
      }
      // The minimized-pages shelf (issue #282) follows: a muted chat leaves it
      // whatever put it there; archiving drops only what was parked by itself.
      if (doc.muted) shelf.muted(`chat:${id}`);
      else if (doc.archived) shelf.archived(`chat:${id}`, true);
      this._changed();
      // What took the row out of view (or brought it back) can be taken back.
      const said = { archive: 'Archived', mute: 'Muted', restore: 'Restored' }[action];
      if (said && before) {
        offerUndo(`${said} · ${chat ? chat.name : ''}`, () => this._setFlags(id, before));
      }
    } catch (_err) {
      // Left as it was; a row swiped out slides back, and the next swipe or
      // tap can retry.
      if (li) this._swipe.slide(li, 0);
    } finally {
      this._flagging = false;
    }
  }

  // Mark as read: POST /chats/<id>/read with an empty body — the server reads
  // the chat up to its newest message and says where the mark now is; the
  // client names no instant of its own. The answer carries the mark it
  // replaced (`previous`) and the one it set (`last_read`): Undo hands both
  // back, and the server puts the old mark back only if the chat's mark is
  // still the one this set (409 otherwise — a reply or another read moved it
  // since, and that stands).
  async _markRead(id, li = null) {
    if (!id || this._flagging) return;
    const chat0 = (this._data && this._data.chats || []).find((c) => c.id === id);
    const before = chat0 ? { unread: chat0.unread, list_group: chat0.list_group } : null;
    this._flagging = true;
    try {
      const doc = await this._postRead(id, {});
      const chat = (this._data && this._data.chats || []).find((c) => c.id === id);
      if (chat) this._adoptRead(chat, doc, 0);
      shelf.observe(`chat:${id}`, { unread: 0 });
      this._changed();
      // Undo needs the mark this replaced; a server that does not say it
      // (one that predates the answer's `previous`) gets no Undo offered.
      if (before && Object.prototype.hasOwnProperty.call(doc, 'previous')) {
        const undo = { restore: doc.previous == null ? null : doc.previous, marked: doc.last_read };
        offerUndo(`Marked read · ${chat ? chat.name : ''}`, () => this._undoRead(id, undo, before));
      }
    } catch (_err) {
      if (li) this._swipe.slide(li, 0);
    } finally {
      this._flagging = false;
    }
  }

  async _undoRead(id, undo, before) {
    if (!id || this._flagging) return;
    this._flagging = true;
    try {
      const doc = await this._postRead(id, undo);
      const chat = (this._data && this._data.chats || []).find((c) => c.id === id);
      if (chat) this._adoptRead(chat, doc, Number(before.unread) || 0);
      shelf.observe(`chat:${id}`, { unread: chat ? Number(chat.unread) || 0 : 0 });
      this._changed();
    } catch (_err) {
      // A 409 is the mark having moved on (a reply, a read elsewhere): what
      // the chat is now is what the next list shows — fetch it now.
      this.load();
    } finally {
      this._flagging = false;
    }
  }

  // The one way this list posts a read; throws on a failed answer (with the
  // status, so a caller can tell a conflict from an outage).
  async _postRead(id, body) {
    const res = await fetch(`/chats/${encodeURIComponent(id)}/read`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  // A read's answer onto the row: its count and group as the server now
  // states them, or — where the answer leaves one out — `unread` as given
  // and the group that follows from it.
  _adoptRead(chat, doc, unread) {
    const d = doc || {};
    chat.unread = typeof d.unread === 'number' ? d.unread : unread;
    if (typeof d.last_read === 'string' || d.last_read === null) chat.last_read = d.last_read;
    chat.list_group = LIST_GROUPS.includes(d.list_group) ? d.list_group : deriveListGroup(chat);
  }

  // A write from here landed: re-render from the list as it now stands, and
  // drop any refresh already on the wire (it answers from before the write).
  _changed() {
    this._dirty = true;
    this._swipe.reset();
    this._sig = '';
    this._epoch += 1;
    if (this._data) this.renderState({ state: 'ok', data: this._data });
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
      /* A swipeable row (swipe-rows.js): the link slides over the layers
         underneath — the quick action on the left (uncovered by a swipe
         right), and on the right either Archive (uncovered by a swipe left
         on Unread and Read) or the tray of buttons. The row needs an opaque
         background of its own to hide them while it rests. */
      ${SWIPE_CSS}
      li.swipe { border-radius: 12px; }
      li.swipe .row { background: var(--card, #151922); }
      li.swipe .row:hover { background: var(--card-2, #1c2230); }
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
      .name .star-mark { margin-right: 5px; }
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
      /* Words for a screen reader that the eye gets from the shape. */
      .vh { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0);
            white-space: nowrap; }
      /* Unread · Read · Archived — full page only; same look as the
         conversations page's filter. */
      .filter { display: flex; background: var(--card-2, #1c2230); border-radius: 12px;
                padding: 3px; margin-bottom: 10px; }
      .filter-tab { flex: 1; background: transparent; border: 0; border-radius: 9px; padding: 7px;
                    color: var(--muted, #8b93a3); cursor: pointer;
                    display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
      .filter-tab:focus-visible { outline: 2px solid var(--accent, #6ea8fe); outline-offset: 1px; }
      .filter-tab.on { background: var(--accent, #6ea8fe); color: #0b0d12; font-weight: 600; }
      /* The number of unread chats, on the Unread tab. */
      .tab-count { min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
                   background: var(--accent, #6ea8fe); color: #0b0d12; font-size: .68rem;
                   font-weight: 700; display: inline-flex; align-items: center; justify-content: center; }
      .filter-tab.on .tab-count { background: #0b0d12; color: var(--accent, #6ea8fe); }
    `;
  }

  // How many chats each tab holds: the payload's own `counts` while the list
  // is as fetched, the rows themselves once a write from here moved one (or
  // where the payload carries no counts).
  _counts(d, all) {
    const given = d && d.counts;
    if (!this._dirty && given && LIST_GROUPS.every((g) => typeof given[g] === 'number')) {
      return { unread: given.unread, read: given.read, archived: given.archived };
    }
    const n = { unread: 0, read: 0, archived: 0 };
    for (const c of all) n[listGroup(c)] += 1;
    return n;
  }

  // The full page's three tabs, a tablist over the one list below it. The
  // Unread tab says how many chats are unread (the rows' badges say how many
  // messages). Starred chats lead whichever list is showing.
  _filterHtml(counts) {
    if (!this._full) return '';
    const LABELS = { unread: 'Unread', read: 'Read', archived: 'Archived' };
    const tab = (scope) => {
      const on = this._scope === scope;
      const n = scope === 'unread' && counts && counts.unread > 0
        ? ` <span class="tab-count">${Number(counts.unread)}</span>` : '';
      return `<button type="button" role="tab" id="chats-tab-${scope}" ` +
        `class="filter-tab${on ? ' on' : ''}" aria-selected="${on}" aria-controls="chats-panel" ` +
        `tabindex="${on ? '0' : '-1'}" data-scope="${scope}">${LABELS[scope]}${n}</button>`;
    };
    return `<div class="filter" role="tablist" aria-label="Chats">${LIST_GROUPS.map(tab).join('')}</div>`;
  }

  // What an empty tab says. An empty list says one thing on every tab.
  _emptyText(any) {
    if (!any || !this._full) return 'No chats yet.';
    return {
      unread: 'Nothing unread.',
      read: 'No read chats.',
      archived: 'No archived chats.',
    }[this._scope];
  }

  body(d) {
    this._data = d;
    const all = Array.isArray(d.chats) ? d.chats.slice() : [];
    // The API contract returns the list ordered by last activity; keep that
    // guarantee client-side too, so a hand-edited fixture cannot scramble it.
    all.sort((a, b) => String((b.last || {}).ts || '').localeCompare(String((a.last || {}).ts || '')));
    // The full page shows one tab's chats; the dashboard card shows every chat
    // that is not archived. The Archived tab is where archived chats remain
    // reachable — and where Restore puts them back.
    const scope = this._full ? this._scope : '';
    const archived = scope === 'archived';
    const inScope = (c) => (scope ? listGroup(c) === scope : listGroup(c) !== 'archived');
    // Starred chats first, each group still by last activity.
    const chats = starredFirst(all.filter(inScope));
    const tabs = this._filterHtml(this._full ? this._counts(d, all) : null);
    // The tabs' panel: what the selected tab shows, rows or its empty line.
    const panel = (inner) => (this._full
      ? `<div role="tabpanel" id="chats-panel" aria-labelledby="chats-tab-${scope}">${inner}</div>`
      : inner);
    if (!chats.length) {
      return `${tabs}${panel(`<p class="muted">${this._emptyText(all.length > 0)}</p>`)}${this._footHtml()}`;
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
      // The count says what it counts in words too, for whoever does not see
      // the pill: "3" alone is not a statement.
      const unread = Number(c.unread) > 0 ? Number(c.unread) : 0;
      const badge = unread
        ? `<span class="unread${c.muted ? ' muted-chat' : ''}" title="${unread} unread">` +
          `${unread}<span class="vh"> unread</span></span>`
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
      const star = starButtonHtml(!!c.starred,
        (set) => `data-flag="${esc(c.id)}" data-set="${set}"`);
      // What a swipe does is in the tray too — Mark read and Archive beside
      // Star and Mute — so a keyboard, which opens the tray with the
      // context-menu key, reaches everything a finger does.
      const tray = star + (archived
        ? act('Restore', 'restore', 'Put this chat back in the list') +
          (c.muted
            ? act('Unmute', 'unmute', 'Stay archived, but come back when it speaks')
            : act('Mute', 'mute', 'Keep it out of the list, even when it speaks')) +
          act('Delete', 'delete',
              'Erase this chat and all its messages from the system')
        : (scope === 'unread'
          ? act('Mark read', 'read', 'Mark everything in this chat as read') : '') +
          act('Archive', 'archive', 'Out of the list until the next message') +
          act('Mute', 'mute',
              'Out of the list, and the next message does not bring it back'));
      // The two sides of a swipe, per tab: right marks read (Unread),
      // restores (Archived) or does nothing (Read — nothing to mark); left
      // archives on Unread and Read, and on Archived uncovers the tray.
      const quick = { unread: 'read', archived: 'restore' }[scope] || '';
      const quickLeft = archived ? '' : 'archive';
      const QUICK_LABEL = { read: 'Mark read', restore: 'Restore' };
      const under = !this._full ? ''
        : (quick ? `<div class="quick ${quick}" aria-hidden="true">${QUICK_LABEL[quick]}</div>` : '') +
          (quickLeft ? `<div class="quick-left ${quickLeft}" aria-hidden="true">Archive</div>` : '') +
          `<div class="tray">${tray}</div>`;
      return (this._full
        ? `<li class="swipe" data-id="${esc(c.id)}"` +
          (quick ? ` data-quick="${quick}"` : '') +
          (quickLeft ? ` data-quick-left="${quickLeft}"` : '') + '>'
        : '<li>') + under +
        `<a class="row${unread ? ' has-unread' : ''}"` +
        (this._full ? ' draggable="false" ' : ' ') +
        `href="/chat.html?id=${encodeURIComponent(c.id)}">` +
        `${avatarHtml(c)}` +
        `<span class="name">${c.starred ? STAR_MARK : ''}${esc(c.name)}` +
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
    return `${tabs}${panel(`<ul class="list">${rows}</ul>`)}${this._footHtml()}`;
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
