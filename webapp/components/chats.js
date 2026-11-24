// Chats card: the messenger mirror's home screen — every channel conversation
// (one peer or group each, and on e-mail one mail thread each) as one row,
// ordered by last activity, with unread badge, channel mark and last-message
// preview. A mail thread's row is named by its subject and carries a line of
// its participants between the name and the preview (docs/mail-chats.md,
// "What a chat shows"). Rows link to the chat's own
// page (chat.html), which renders the full mirror beside its companion thread.
// With the `full` attribute (chats.html) the list drops the dashboard cap and
// splits into three tabs — Unread · Read · Archived — by the server's
// `list_group`. Archived chats are excluded everywhere else; `muted` only
// changes the badge treatment here (its real meaning — no Web Push, and no
// un-archive on a new inbound message — is the server's).
//
// **Unread** is a chat whose latest inbound message has been neither replied
// to nor marked read; **Read** is one where it has. Opening a chat reads
// nothing: a chat leaves Unread by a reply (from the dashboard; from the
// phone too on the channels that echo it — Signal, WhatsApp, Telegram, not
// SMS) or by Mark as read — a swipe here, or the button under its latest
// message on the chat page — and a new inbound message brings it back. A
// mark from here is about the row as it is shown: it names the row's
// `inbound_seq`, and the server refuses it when a message has landed that
// the row does not show yet. The server says which group a chat is
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
//                 swipe left   reveal the tray; a full swipe (or a fling)
//                              archives at once
//   Read tab      swipe right  nothing (there is nothing to mark)
//                 swipe left   reveal the tray; a full swipe (or a fling)
//                              archives at once
//   Archived tab  swipe right  restore at once
//                 swipe left   reveal the tray
//
// The tray offers the same actions on every tab and every channel: Star ·
// Mark read (Unread only) · Archive · Mute — on Archived, Restore · Mute or
// Unmute in their place — · Spam · Delete. **Spam** is the one exception: it
// is on mail threads only (a messenger chat has no Junk folder; it is muted
// instead). POST /chats/<id>/spam has the mailbox move the thread's inbound
// mails to its Junk folder, and only then mutes and archives the chat.
// Restore brings a spam chat back like any muted one; its mails stay in Junk.
// Right-click and the context-menu key open the tray too (the keyboard's way
// to everything a swipe does). **Star** keeps a chat at the top of whichever
// list it is in, and does nothing else.
//
// Every action that takes a row out of view offers Undo for UNDO_MS (15 s;
// the toast stays while a finger or the pointer is on it). Mark read,
// Archive and Mute are made at once, and their Undo writes the previous state
// back (Mark read's the previous read mark). **Spam** is deferred instead:
// the row leaves at once ("Marked as spam · Undo") and NOTHING is sent until
// the window ends; Undo within it sends nothing and puts the row back; a page
// hidden or left meanwhile sends it at once (fetch keepalive) — leaving never
// cancels what the user did. **Delete** has no Undo: it asks for a second
// tap, its armed button saying what that does — "Delete for good?", and on a
// mail thread "Move mails to Trash?" — and erases the chat from the whole
// system (every message and file in the ledger, its companion thread; a later
// message from the peer starts a new chat). On a mail thread it first moves
// the thread's mails to the mailbox's Trash (nothing is expunged there). An
// action that fails brings the row back, marked failed, with the server's
// own one-sentence reason in place of its preview, and — for Delete and Spam
// — a Retry in its tray (opened). The chat page's ⋮ has Delete (two taps
// there too) and Spam, which it hands over to this list (HANDOFF_KEY): it
// leaves for chats.html, which takes the Spam up with the same Undo.
//
// **Multi-select.** A long press on a row (or the list's Select) starts
// selection mode with that row selected — so the long press no longer opens
// the tray; the partial swipe, right-click and the context-menu key still
// do. In the mode a tap (Space on a focused row) toggles a row, Shift-click
// on a wide screen selects a range, and a bar says how many are selected and
// offers what the tab's tray offers, for all of them: Mark read, Archive,
// Mute, Restore / Unmute (Archived), Star / Unstar, Spam (only when every
// selected chat is a mail thread) and Delete. Esc, Cancel, the back gesture
// (the mode is a history entry) and a tab switch leave it; a selection is one
// tab's, and a row that leaves the tab (a poll) leaves the selection. A bulk
// action is one request per chat (BULK_CONCURRENCY at a time, no bulk
// endpoint) and ONE Undo for the batch ("Archived 7 · Undo") that writes
// every chat's previous state back; chats whose request failed come back
// marked failed with their reasons, and the toast says so ("Archived 5 of 7
// — 2 failed"). A bulk Delete keeps the two taps (the bar's Delete arms:
// "Delete 7 chats? Mails move to the Trash"); a bulk Spam is deferred like a
// single one.
//
// The full page's **＋** starts a new conversation: a short menu of what GET
// /chats/compose offers (today *New e-mail*, where a mail gateway runs), each
// opening the compose sheet (components/compose-sheet.js), which goes to the
// new chat once it is sent or queued. With nothing offered there is no ＋.
//
// The list comes from the gateway's GET /chats (the default `src`, still
// overridable by attribute) — SPARQL over the message ledgers merged with the
// live overlay and the chat state. The response shape is documented in
// webapp/README.md, "Messenger chats"; the reference documents under
// webapp/data/chats* mirror it for tests. The card refreshes on an ambient
// cadence and keeps its last rendered state over a failed fetch (a store blip
// must not blank the list).

import {
  RetinueCard, esc, fmtAge, isWideFrame, onFrameChange, starredFirst, deepActiveElement, onPressOutside,
} from './base.js';
import { shelf } from './shelf-store.js';
import { SwipeRows, SWIPE_CSS, STAR_MARK, offerUndo, showNotice, starButtonHtml, UNDO_MS } from './swipe-rows.js';
import { openComposeSheet } from './compose-sheet.js';

const LIST_URL = '/chats';
// What the full page's ＋ can start (docs/mail-chats.md, "Writing"): the
// channels whose gateway takes a new conversation, with their accounts.
const COMPOSE_URL = '/chats/compose';
// Rows shown on the dashboard card before "All chats →" takes over — the same
// cap logic as the conversations card: only the phone layout, where each row
// lengthens the page, caps the list.
const MAX_CARD_CHATS = 5;
// Ambient refresh: the card carries summaries, not the open thread — a gentler
// cadence than the conversations card's 4s poll is enough (the server caches
// the SPARQL skeleton between polls anyway).
const REFRESH_MS = 15000;

// Where the chat page leaves a Spam from its menu for this list to carry
// out, with the list's Undo: {id, kind: 'spam', name, at}.
export const HANDOFF_KEY = 'retinue-chat-pending-action';

// The one action made only once its Undo window has ended (offerUndo's
// `commit`): Spam. Delete asks for a second tap instead, and is made then.
const DEFERRED = { spam: true };

// What each action is called: in the toast (`done`), on a row whose action
// failed (`failed`), and on the button that tries it again (`retry`).
const ACTIONS = {
  read: { done: 'Marked read', failed: 'Not marked read' },
  archive: { done: 'Archived', failed: 'Not archived' },
  mute: { done: 'Muted', failed: 'Not muted' },
  unmute: { done: 'Unmuted', failed: 'Not unmuted' },
  restore: { done: 'Restored', failed: 'Not restored' },
  star: { done: 'Starred', failed: 'Not starred' },
  unstar: { done: 'Unstarred', failed: 'Not unstarred' },
  spam: { done: 'Marked as spam', failed: 'Not marked as spam', retry: 'Retry spam' },
  delete: { done: 'Deleted', failed: 'Not deleted', retry: 'Retry delete' },
};

// The flags each flag action writes, exactly as a conversation has them.
const FLAGS = {
  archive: { archived: true },
  mute: { muted: true },
  // Stays archived — it only lets the next message bring the chat back.
  unmute: { muted: false },
  restore: { archived: false, muted: false },
  star: { starred: true },
  unstar: { starred: false },
};

// A bulk action runs one request per chat, this many at a time.
export const BULK_CONCURRENCY = 4;

// `fn` over `items`, at most `limit` at once; the results in item order (a
// throw is a result too: {ok: false, reason}).
export async function eachBounded(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const k = next;
      next += 1;
      try {
        out[k] = await fn(items[k], k);
      } catch (err) {
        out[k] = { ok: false, reason: String((err && err.message) || err) };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

// Hand a Spam to the chat list (the chat page's menu): true when it was put
// where the list will find it — the caller then goes there.
export function handOffChatAction(id, kind, name) {
  if (!id || !DEFERRED[kind]) return false;
  try {
    localStorage.setItem(HANDOFF_KEY, JSON.stringify({ id, kind, name: name || '', at: Date.now() }));
    return localStorage.getItem(HANDOFF_KEY) !== null;
  } catch (_e) {
    return false;
  }
}

// The action a chat page handed over, taken (it is this caller's now); null
// for none.
function takeHandoff() {
  try {
    const raw = localStorage.getItem(HANDOFF_KEY);
    if (raw === null) return null;
    localStorage.removeItem(HANDOFF_KEY);
    const h = JSON.parse(raw);
    return h && typeof h.id === 'string' && DEFERRED[h.kind] ? h : null;
  } catch (_e) {
    return null;
  }
}

// The one sentence a failed POST answers with (`error`), as the user is
// shown it; `fallback` where the answer gives none.
export async function reasonOf(res, fallback) {
  try {
    const doc = await res.json();
    const said = doc && typeof doc.error === 'string' ? doc.error.trim() : '';
    if (said) return said.charAt(0).toUpperCase() + said.slice(1);
  } catch (_e) { /* no JSON: the fallback */ }
  return fallback;
}

// Send a chat's Delete or Spam: POST /chats/<id>/<kind>. `{ok, doc}` or `{ok:
// false, reason, unconfigured}` — never throws. `keepalive` for a page on its
// way out. Shared with the chat page (its fallback when nothing can be handed
// over).
export async function commitChatAction(id, kind, keepalive = false) {
  let res;
  try {
    res = await fetch(`/chats/${encodeURIComponent(id)}/${kind}`, { method: 'POST', keepalive: !!keepalive });
  } catch (_e) {
    return { ok: false, reason: 'The dashboard could not be reached — nothing was changed.', unconfigured: false };
  }
  if (res.ok) {
    let doc = {};
    try { doc = await res.json(); } catch (_e) { /* an empty answer is still a yes */ }
    return { ok: true, doc: doc || {} };
  }
  // A 503 on Delete is a deployment without the erase capability: retrying
  // will not help, and the reason says so.
  const unconfigured = kind === 'delete' && res.status === 503;
  const fallback = unconfigured ? 'Delete not set up'
    : `The server answered ${res.status} — nothing was changed.`;
  return { ok: false, reason: await reasonOf(res, fallback), unconfigured };
}

// Channel marks: no brand assets in the shell, so a lettered dot in the
// channel's recognisable colour does the telling.
export const CHANNELS = {
  signal: { label: 'Signal', mark: 'S', color: '#3a76f0' },
  whatsapp: { label: 'WhatsApp', mark: 'W', color: '#25d366' },
  telegram: { label: 'Telegram', mark: 'T', color: '#2aabee' },
  sms: { label: 'SMS', mark: 'M', color: '#8e7cc3' },
  email: { label: 'E-mail', mark: '@', color: '#d0603f' },
};

// Whether a chat is a mail thread: its name is a subject, its peer a person
// who is not the chat, and an outbound message "from the device" was sent
// from a mail client rather than from the user's phone.
export function isMail(chat) {
  return !!chat && chat.channel === 'email';
}

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
//
// A mail chat's key is its thread's root Message-ID, which names the thread
// and not the person: there the summary's `peer` (the latest inbound sender,
// or whom the user wrote to) is the key, by address, so one correspondent
// keeps one colour across every thread with them — and the initials are that
// person's too: the disc says who, the row's name what about.
export function avatarKey(chat) {
  const c = chat || {};
  const addr = c.peer && typeof c.peer.address === 'string' ? c.peer.address.trim().toLowerCase() : '';
  return `${c.channel || ''}\u0000${addr || c.key || c.id || ''}`;
}

export function avatarInitials(chat) {
  const c = chat || {};
  const p = c.peer && typeof c.peer === 'object' ? c.peer : null;
  const who = p && (String(p.name || '').trim() || String(p.address || '').trim());
  return initials(who || c.name);
}

export function avatarHtml(chat) {
  return `<span class="av" style="background:${colorFor(avatarKey(chat))}" aria-hidden="true">` +
    `${esc(avatarInitials(chat))}${channelMarkHtml(chat.channel)}</span>`;
}

// A mail chat's participants as one line of plain text (the caller escapes
// it): each by the name the address book or the mail gave, else the address,
// and "+n" for those the server's cap left out. '' for a chat that names
// none — every messenger chat.
export function participantsText(chat) {
  const c = chat || {};
  const list = Array.isArray(c.participants) ? c.participants : [];
  const names = list
    .map((p) => (p && typeof p === 'object'
      ? (String(p.name || '').trim() || String(p.address || '').trim()) : ''))
    .filter(Boolean);
  const n = Number(c.participants_more);
  const more = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  if (!names.length) return more ? `+${more}` : '';
  return names.join(', ') + (more ? ` +${more}` : '');
}

// The one-line preview under the chat name, messenger-home style: who said the
// last thing, then what. Outbound gets its author ("You", the agent's name,
// "You (phone)" for an own-device echo — "You (mail client)" on e-mail, where
// the device is whichever client sent it); group inbound gets the sender's
// first name, and so does every inbound mail, whose row is named by its
// subject and says nothing else about who wrote. An image message shows a
// camera mark before any caption; a mail removed in a mail client says so in
// place of the text it no longer has.
// What ＋ can start, from GET /chats/compose: `{channel, label, accounts}` per
// channel whose gateway takes a new conversation.
function composeOffers(doc) {
  return (Array.isArray(doc && doc.channels) ? doc.channels : [])
    .filter((c) => c && typeof c.channel === 'string' && c.channel)
    .map((c) => ({
      channel: c.channel,
      label: typeof c.label === 'string' && c.label.trim() ? c.label.trim()
        : (CHANNELS[c.channel] || { label: c.channel }).label,
      accounts: Array.isArray(c.accounts) ? c.accounts.filter((a) => typeof a === 'string' && a) : [],
    }));
}

export function previewHtml(chat) {
  const last = chat.last || {};
  const mail = isMail(chat);
  let who = '';
  if (last.direction === 'out') {
    who = last.author === 'agent' ? 'Ara'
      : last.author === 'device' ? (mail ? 'You (mail client)' : 'You (phone)') : 'You';
  } else if ((chat.group || mail) && last.sender_name) {
    who = String(last.sender_name).trim().split(/\s+/)[0];
  }
  const lead = who ? `<span class="pv-who">${esc(who)}:</span> ` : '';
  if (last.removed) return `${lead}<span class="pv-removed">Removed in a mail client</span>`;
  // A send that waits for approval (a new mail that exists only as one, so
  // far) says so before its words: it has not gone out. Where the server
  // says what became of one that was let go (`status`), that is said instead.
  if (last.pending) {
    const state = last.status === 'failed' ? (last.outcome_unknown === true ? 'Not confirmed:' : 'Not sent:')
      : (last.status === 'sending' ? 'Sending:' : 'Waiting for approval:');
    return `${lead}<span class="pv-pending">${state}</span> ${esc(last.text || '')}`;
  }
  const img = last.kind === 'image' ? '<span class="pv-img" aria-label="Image">&#128247;</span> ' : '';
  const text = last.text || (last.kind === 'image' ? 'Photo' : '');
  return lead + img + esc(text);
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
    // A deferred Spam (id → 'spam'): rows that left the list and whose
    // action is not sent yet; and the rows whose action failed (id → {kind,
    // reason, unconfigured}), shown back with the server's reason.
    this._pending = new Map();
    this._failed = new Map();
    // Multi-select (full page): whether the list is selecting, which rows,
    // the row a Shift-click range runs from, and whether the bar's Delete is
    // armed (its second tap deletes).
    this._selecting = false;
    this._selected = new Set();
    this._anchor = null;
    this._bulkArmed = false;
    this._shownIds = [];
    this._swipe = new SwipeRows(this, {
      onQuick: (id, action, el) => (action === 'read'
        ? this._markRead(id, el) : this._setFlags(id, action, el)),
      isBusy: () => !!this._flagging,
      onLongPress: (id) => this._enterSelect(id),
      selecting: () => this._selecting,
      onSelect: (id, _el, e) => this._toggleSelect(id, e),
    });
    // The back gesture leaves selection mode (the mode is a history entry).
    this._onPop = () => { if (this._selecting) this._exitSelect(false); };
    this._onKey = (e) => {
      if (e.key === 'Escape' && this._selecting) { e.preventDefault(); this._exitSelect(); }
    };
    if (this._full) {
      window.addEventListener('popstate', this._onPop);
      document.addEventListener('keydown', this._onKey);
    }
    // Crossing the layout breakpoint changes how many rows fit (cap vs all).
    this._offFrame = onFrameChange(() => {
      if (this._data) this.renderState({ state: 'ok', data: this._data });
    });
    // What ＋ offers (null: not known yet) and whether its menu is open —
    // on the element, so a render of the list keeps the menu as it was.
    this._compose = null;
    this._newOpen = false;
    super.connectedCallback();
    this._timer = setInterval(() => this.load(), REFRESH_MS);
    // A Spam the chat page's menu handed over, with its Undo here.
    if (this._full) this._adoptHandoff();
  }

  // Take up what a chat page left (handOffChatAction): the row is hidden at
  // once and the action waits for its Undo window like one made here. One
  // left long ago — the page that would have carried it out never loaded —
  // is what the user did all the same: made now.
  _adoptHandoff() {
    const h = takeHandoff();
    if (!h) return;
    const age = Date.now() - Number(h.at || 0);
    if (!(age >= 0 && age < UNDO_MS + 60000)) {
      this._commitMany([h.id], h.kind, false);
      return;
    }
    this._defer(h.id, h.kind, h.name);
  }

  disconnectedCallback() {
    window.removeEventListener('popstate', this._onPop);
    document.removeEventListener('keydown', this._onKey);
    if (this._newAway) this._newAway();
    this._newAway = null;
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
    if (this._full && this._compose === null) this._loadCompose();
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
      // A rebuild under a finger mid-swipe would drop the row it is dragging,
      // and one under the open ＋ menu the item about to be pressed; the
      // next tick picks the change up.
      if (this._swipe.dragging || this._newOpen) return;
      // A failed row whose chat the list no longer has is nothing to show.
      for (const id of [...this._failed.keys()]) {
        if (!(data.chats || []).some((c) => c.id === id)) this._failed.delete(id);
      }
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
        else if (DEFERRED[action]) this._defer(id, action);
        else if (action === 'read') this._markRead(id);
        else this._setFlags(id, action);
      }));
    this.shadowRoot.querySelectorAll('[data-bulk]').forEach((el) =>
      el.addEventListener('click', (e) => {
        e.preventDefault();
        const what = el.getAttribute('data-bulk');
        if (what === 'cancel') this._exitSelect();
        else if (what === 'start') this._enterSelect(null);
        else this._bulk(what);
      }));
    if (this._full) this._swipe.wire(this.shadowRoot);
    this._wireNew();
  }

  // ── ＋: a new conversation ─────────────────────────────────────────────────
  // What it can start is the server's answer (GET /chats/compose), asked
  // once; a failed ask is made again with the next refresh, and an endpoint
  // that is not there (a gateway older than it) is an answer: nothing.
  async _loadCompose() {
    if (this._composeBusy || this._compose !== null) return;
    this._composeBusy = true;
    try {
      const res = await fetch(COMPOSE_URL, { cache: 'no-store' });
      if (res.status === 404) {
        this._compose = [];
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      this._compose = composeOffers(await res.json());
      if (this._compose.length && this._data) this.renderState({ state: 'ok', data: this._data });
    } catch (_err) {
      // No ＋ for now; the next refresh asks again.
    } finally {
      this._composeBusy = false;
    }
  }

  // The mailboxes an offer names are the ones that can send at the moment it
  // was asked for. As the ＋ is opened they are asked for again, so a mailbox
  // that was away when the page loaded and has come back can be written from
  // — a mail the compose sheet kept for it waits for exactly that. Only the
  // mailboxes of the offers on the menu are taken, in place: the menu under
  // the user's finger keeps its entries.
  async _refreshCompose() {
    if (this._composeBusy || !Array.isArray(this._compose) || !this._compose.length) return;
    this._composeBusy = true;
    try {
      const res = await fetch(COMPOSE_URL, { cache: 'no-store' });
      if (!res.ok) return;
      const fresh = composeOffers(await res.json());
      for (const offer of this._compose) {
        const now = fresh.find((f) => f.channel === offer.channel);
        if (now) offer.accounts = now.accounts;
      }
    } catch (_err) {
      // What the page was told before stands.
    } finally {
      this._composeBusy = false;
    }
  }

  // The ＋ and its menu: a disclosure (a button that shows and hides a short
  // list of ordinary buttons, as the chat page's ⋮ is), beside the tabs.
  _newHtml() {
    const offers = this._full && Array.isArray(this._compose) ? this._compose : [];
    if (!offers.length) return '';
    const open = !!this._newOpen;
    const what = (o) => (/^e-mail$/i.test(o.label) ? 'e-mail' : o.label);
    return `<div class="new-wrap" data-new-wrap>` +
      `<button type="button" class="new-btn" data-new aria-expanded="${open}" aria-controls="chats-new" ` +
      `title="Start a new conversation" aria-label="Start a new conversation">&#65291;</button>` +
      `<div class="new-menu" id="chats-new" data-new-menu${open ? '' : ' hidden'}>` +
      offers.map((o, i) => `<button type="button" class="new-item" data-new-offer="${i}">` +
        `New ${esc(what(o))}</button>`).join('') +
      `</div></div>`;
  }

  _wireNew() {
    const root = this.shadowRoot;
    // The listeners of a menu open before this render watched nodes that are
    // gone: set up again around the new ones.
    if (this._newAway) { this._newAway(); this._newAway = null; }
    const btn = root.querySelector('[data-new]');
    if (!btn) { this._newOpen = false; return; }
    btn.addEventListener('click', () => {
      this._toggleNew();
      if (this._newOpen) this._refreshCompose();
    });
    root.querySelectorAll('[data-new-offer]').forEach((el) =>
      el.addEventListener('click', () => this._startNew(Number(el.getAttribute('data-new-offer')))));
    if (this._newOpen) this._toggleNew(true);
  }

  _toggleNew(open) {
    const root = this.shadowRoot;
    const menu = root && root.querySelector('[data-new-menu]');
    const btn = root && root.querySelector('[data-new]');
    const show = menu && btn ? (open === undefined ? menu.hidden : !!open) : false;
    if (menu && btn) {
      // Hiding the item that has focus would drop it to the document.
      if (!show && !menu.hidden && menu.contains(deepActiveElement())) btn.focus();
      menu.hidden = !show;
      btn.setAttribute('aria-expanded', String(show));
    }
    this._newOpen = show;
    if (show && !this._newAway) {
      // Any press outside the menu, or Esc, shuts it.
      const wrap = root.querySelector('[data-new-wrap]');
      const onKey = (e) => {
        if (e.key !== 'Escape') return;
        this._toggleNew(false);
        const b = this.shadowRoot.querySelector('[data-new]');
        if (b) b.focus();
      };
      const offPress = onPressOutside(wrap, () => this._toggleNew(false), { capture: true });
      document.addEventListener('keydown', onKey);
      this._newAway = () => { offPress(); document.removeEventListener('keydown', onKey); };
    } else if (!show && this._newAway) {
      this._newAway();
      this._newAway = null;
    }
  }

  // One of ＋'s offers picked: the compose sheet for it, and the focus back on
  // the ＋ (as this list now renders it) when the sheet closes.
  _startNew(i) {
    const offer = Array.isArray(this._compose) ? this._compose[i] : null;
    this._toggleNew(false);
    if (!offer) return;
    openComposeSheet(offer, () => {
      const b = this.shadowRoot && this.shadowRoot.querySelector('[data-new]');
      if (b) b.focus();
    });
  }

  // Switch the full page's tab; the list is already here, so no fetch.
  _setScope(scope) {
    if (!LIST_GROUPS.includes(scope) || scope === this._scope) return;
    // A selection is one tab's: switching tabs ends it.
    if (this._selecting) this._exitSelect(true, false);
    this._scope = scope;
    this._swipe.reset();
    if (this._data) this.renderState({ state: 'ok', data: this._data });
  }

  _chat(id) {
    return (this._data && this._data.chats || []).find((c) => c.id === id) || null;
  }

  // Spam, deferred: the row leaves now, and the action is sent only when the
  // Undo window ends (or the page is hidden or left: then at once,
  // keepalive). Undo within it sends nothing and puts the row back.
  _defer(id, kind, name = '') {
    if (!id || !DEFERRED[kind] || this._pending.has(id)) return;
    const chat = this._chat(id);
    // Spam is a mail thread's alone (the server refuses it for any other).
    if (kind === 'spam' && chat && !isMail(chat)) return;
    this._pending.set(id, kind);
    this._failed.delete(id);
    // Hidden by body() itself, so a list on the wire may still land: no new
    // epoch (it would drop the first list of a page that took a handoff).
    this._hidden();
    offerUndo(`${ACTIONS[kind].done} · ${chat ? chat.name : name}`, () => {
      this._pending.delete(id);
      this._hidden();
    }, { commit: (keepalive) => this._commitMany([id], kind, keepalive) });
  }

  // Deferred actions go out, one request per chat (bounded). Each that
  // succeeded is in Archived, muted (Spam); each that failed comes back,
  // marked failed with the server's reason — and a single row's tray, with
  // Retry, is opened.
  async _commitMany(ids, kind, keepalive = false) {
    ids.forEach((id) => this._pending.set(id, kind));
    const outs = await eachBounded(ids, BULK_CONCURRENCY, (id) => commitChatAction(id, kind, keepalive));
    ids.forEach((id, k) => {
      this._pending.delete(id);
      const out = outs[k] || { ok: false, reason: 'No answer' };
      if (out.ok) {
        const chat = this._chat(id);
        if (chat) {
          chat.archived = out.doc.archived !== false;
          chat.muted = out.doc.muted !== false;
          chat.list_group = deriveListGroup(chat);
        }
        this._failed.delete(id);
        shelf.muted(`chat:${id}`);
      } else {
        this._failed.set(id, { kind, reason: out.reason, unconfigured: !!out.unconfigured });
      }
    });
    this._changed();
    if (ids.length === 1 && !outs[0].ok) {
      const li = this._swipe.rowEl(ids[0]);
      if (li) this._swipe.open(li, false);
    }
    return outs;
  }

  // Delete cannot be undone, so the first tap only arms it (swipe-rows.js):
  // the button then says what a second tap does — "Delete for good?", or on
  // a mail thread "Move mails to Trash?" — and the second deletes.
  _confirmDelete(id, btn) {
    if (this._swipe.confirm(id, btn)) this._deleteChat(id);
  }

  async _deleteChat(id) {
    if (!id || this._flagging) return;
    this._flagging = true;
    const busy = this._swipe.rowEl(id);
    if (busy) busy.classList.add('busy');
    try {
      await this._deleteMany([id]);
    } finally {
      this._flagging = false;
    }
    if (this._failed.has(id)) {
      const li = this._swipe.rowEl(id);
      if (li) this._swipe.open(li, false);
    }
  }

  // Erase chats, one request each (bounded): the ones erased leave the list;
  // each that was not stays, marked failed with the server's reason (the
  // gateway answers 502 and touches nothing of the chat when its messages
  // cannot all go; a 503 is a deployment without the erase capability).
  async _deleteMany(ids) {
    const outs = await eachBounded(ids, BULK_CONCURRENCY, (id) => commitChatAction(id, 'delete'));
    const gone = new Set();
    ids.forEach((id, k) => {
      const out = outs[k] || { ok: false, reason: 'No answer' };
      if (out.ok) {
        gone.add(id);
        this._failed.delete(id);
        shelf.muted(`chat:${id}`);
      } else {
        this._failed.set(id, { kind: 'delete', reason: out.reason, unconfigured: !!out.unconfigured });
      }
    });
    if (gone.size && this._data && Array.isArray(this._data.chats)) {
      this._data.chats = this._data.chats.filter((c) => !gone.has(c.id));
    }
    this._changed();
    return outs;
  }

  // ── Multi-select ───────────────────────────────────────────────────────────
  // A long press on a row (or the list's Select) starts it, with that row
  // selected; a tap — or Space on a focused row — then toggles a row, and a
  // Shift-click on a wide screen selects the range from the last one. The
  // bar says how many and offers what the tab's tray offers, for all of
  // them. Esc, Cancel, the back gesture and a tab switch end it; a row that
  // leaves the tab (a poll) leaves the selection.
  _enterSelect(id) {
    if (!this._full) return;
    if (!this._selecting) {
      this._selecting = true;
      this._selected = new Set();
      try { history.pushState({ ...(history.state || {}), chatsSelect: true }, ''); } catch (_e) { /* none */ }
    }
    if (id) {
      this._selected.add(id);
      this._anchor = id;
    }
    this._bulkArmed = false;
    this._swipe.reset();
    this._rerender();
  }

  // `back`: take the mode's history entry off too (not when the back
  // gesture already did); `render` false for a caller that renders next.
  _exitSelect(back = true, render = true) {
    if (!this._selecting) return;
    this._selecting = false;
    this._selected = new Set();
    this._anchor = null;
    this._bulkArmed = false;
    try {
      if (back && history.state && history.state.chatsSelect) history.back();
    } catch (_e) { /* none */ }
    if (render) this._rerender();
  }

  _toggleSelect(id, e) {
    if (!this._selecting || !id) return;
    const range = e && e.shiftKey && this._anchor && isWideFrame()
      && this._shownIds.includes(this._anchor) && this._shownIds.includes(id);
    if (range) {
      const a = this._shownIds.indexOf(this._anchor);
      const b = this._shownIds.indexOf(id);
      this._shownIds.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((x) => this._selected.add(x));
    } else if (this._selected.has(id)) {
      this._selected.delete(id);
    } else {
      this._selected.add(id);
    }
    this._anchor = id;
    this._bulkArmed = false;
    this._rerender();
  }

  // A row hidden or shown again by a deferred action: the tabs count rows.
  _hidden() {
    this._dirty = true;
    this._swipe.reset();
    this._rerender();
  }

  _rerender() {
    if (this._data) this.renderState({ state: 'ok', data: this._data });
  }

  // What the bar offers on this tab for these chats: what the tray offers.
  _bulkActions(chats) {
    const all = (f) => chats.length > 0 && chats.every(f);
    const out = [];
    if (this._scope === 'archived') {
      out.push('restore', all((c) => c.muted) ? 'unmute' : 'mute');
    } else {
      if (this._scope === 'unread') out.push('read');
      out.push('archive', 'mute');
    }
    out.push(all((c) => c.starred) ? 'unstar' : 'star');
    // Spam only where every chat selected is a mail thread.
    if (all(isMail)) out.push('spam');
    out.push('delete');
    return out;
  }

  _barHtml(inScope) {
    if (!this._full) return '';
    if (!this._selecting) {
      return '<button type="button" class="sel-start" data-bulk="start" ' +
        'title="Select several chats to act on them together">Select</button>';
    }
    const chats = inScope.filter((c) => this._selected.has(c.id));
    const n = chats.length;
    const LABEL = { read: 'Mark read', archive: 'Archive', mute: 'Mute', unmute: 'Unmute', restore: 'Restore',
      star: '&#9733; Star', unstar: 'Unstar', spam: 'Spam', delete: 'Delete' };
    const btn = (what) => {
      if (what === 'delete' && this._bulkArmed) {
        const mails = chats.some(isMail);
        return `<button type="button" class="bulk delete confirm" data-bulk="delete">Delete ${n} chat${
          n === 1 ? '' : 's'}? ${mails ? 'Mails move to the Trash' : 'For good'}</button>`;
      }
      return `<button type="button" class="bulk ${what}" data-bulk="${what}"${n ? '' : ' disabled'}>${LABEL[what]}</button>`;
    };
    return `<div class="select-bar" role="toolbar" aria-label="Selected chats">` +
      `<span class="sel-count" role="status">${n} selected</span>` +
      `<span class="sel-acts">${this._bulkActions(chats).map(btn).join('')}</span>` +
      `<button type="button" class="bulk cancel" data-bulk="cancel">Cancel</button></div>`;
  }

  // One action on every selected chat: one request per chat (bounded), and
  // ONE Undo for the batch that writes every chat's previous state back.
  // Delete arms first (the bar's button says what the second tap does);
  // Spam is deferred like a single one. The chats whose request failed come
  // back marked failed, each with its reason, and the toast says how many.
  async _bulk(what) {
    if (!this._selecting || this._flagging) return;
    const inScope = this._scopeChats();
    const chats = inScope.filter((c) => this._selected.has(c.id));
    if (!chats.length || !this._bulkActions(chats).includes(what)) return;
    const ids = chats.map((c) => c.id);
    if (what === 'delete') {
      if (!this._bulkArmed) {
        this._bulkArmed = true;
        this._rerender();
        return;
      }
      this._exitSelect(true, false);
      this._flagging = true;
      try {
        await this._deleteMany(ids);
      } finally {
        this._flagging = false;
      }
      return;
    }
    this._exitSelect(true, false);
    if (what === 'spam') {
      ids.forEach((id) => { this._pending.set(id, 'spam'); this._failed.delete(id); });
      this._hidden();
      offerUndo(`${ACTIONS.spam.done} ${ids.length}`, () => {
        ids.forEach((id) => this._pending.delete(id));
        this._hidden();
      }, {
        commit: async (keepalive) => {
          const outs = await this._commitMany(ids, 'spam', keepalive);
          const ok = outs.filter((o) => o && o.ok).length;
          // A page on its way out has no one to tell.
          if (!keepalive && ok < ids.length) {
            showNotice(`${ACTIONS.spam.done} ${ok} of ${ids.length} — ${ids.length - ok} failed`);
          }
        },
      });
      return;
    }
    this._rerender();
    this._flagging = true;
    let outs;
    try {
      outs = await eachBounded(ids, BULK_CONCURRENCY, (id) => this._applyOne(id, what));
    } finally {
      this._flagging = false;
    }
    ids.forEach((id, k) => {
      if (outs[k] && outs[k].ok) this._failed.delete(id);
      else this._failed.set(id, { kind: what, reason: (outs[k] && outs[k].reason) || 'No answer' });
    });
    this._changed();
    const undos = outs.filter((o) => o && o.ok && o.undo).map((o) => o.undo);
    const ok = outs.filter((o) => o && o.ok).length;
    const said = ok === ids.length ? `${ACTIONS[what].done} ${ok}`
      : `${ACTIONS[what].done} ${ok} of ${ids.length} — ${ids.length - ok} failed`;
    if (!undos.length) {
      if (ok < ids.length) showNotice(said);
      return;
    }
    offerUndo(said, async () => {
      this._flagging = true;
      try {
        await eachBounded(undos, BULK_CONCURRENCY, (undo) => undo());
      } finally {
        this._flagging = false;
      }
      this._changed();
    });
  }

  // One chat of a bulk action: `{ok, undo}` (undo writes its previous state
  // back) or `{ok: false, reason}` — the server's sentence where it gave one.
  async _applyOne(id, what) {
    const chat = this._chat(id);
    if (what === 'read') {
      const seen = chat ? chat.inbound_seq : undefined;
      const before = chat ? { unread: chat.unread } : { unread: 0 };
      const res = await fetch(`/chats/${encodeURIComponent(id)}/read`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Number.isInteger(seen) ? { seen } : {}),
      });
      if (!res.ok) {
        return { ok: false, reason: await reasonOf(res, res.status === 409
          ? 'A message came in since — nothing was marked read.' : `The server answered ${res.status}.`) };
      }
      const doc = await res.json();
      if (chat) this._adoptRead(chat, doc, 0);
      shelf.observe(`chat:${id}`, { unread: chat ? Number(chat.unread) || 0 : 0 });
      const undo = Object.prototype.hasOwnProperty.call(doc || {}, 'previous')
        ? async () => {
          const r = await fetch(`/chats/${encodeURIComponent(id)}/read`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ restore: doc.previous == null ? null : doc.previous, marked: doc.last_read }),
          });
          const c = this._chat(id);
          if (r.ok && c) this._adoptRead(c, await r.json(), Number(before.unread) || 0);
          return { ok: r.ok };
        } : null;
      return { ok: true, undo };
    }
    const flags = FLAGS[what];
    const before = chat ? { archived: !!chat.archived, muted: !!chat.muted, starred: !!chat.starred } : null;
    const out = await this._postFlags(id, flags);
    if (!out.ok) return out;
    return {
      ok: true,
      undo: before ? async () => this._postFlags(id, before) : null,
    };
  }

  // POST /chats/<id>/flags, and the answer onto the row and the shelf:
  // `{ok}` or `{ok: false, reason}`.
  async _postFlags(id, flags) {
    const res = await fetch(`/chats/${encodeURIComponent(id)}/flags`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(flags),
    });
    if (!res.ok) return { ok: false, reason: await reasonOf(res, `The server answered ${res.status}.`) };
    const doc = await res.json();
    const chat = this._chat(id);
    if (chat) {
      chat.archived = !!doc.archived; chat.muted = !!doc.muted; chat.starred = !!doc.starred;
      chat.list_group = deriveListGroup(chat);
    }
    if (doc.muted) shelf.muted(`chat:${id}`);
    else if (doc.archived) shelf.archived(`chat:${id}`, true);
    return { ok: true, doc };
  }

  // The chats of the tab showing, as body() lists them.
  _scopeChats() {
    const all = (this._data && Array.isArray(this._data.chats) ? this._data.chats : [])
      .filter((c) => !this._pending.has(c.id));
    return all.filter((c) => listGroup(c) === this._scope);
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
      this._failed.delete(id);
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

  // Mark as read: POST /chats/<id>/read with `{seen}`, the `inbound_seq` of
  // the row as this list shows it. The list is up to REFRESH_MS old, and a
  // message that landed since is one the user has not been shown: the server
  // then marks nothing and answers 409, the row slides back, and the list is
  // fetched again so the row shows what came in. The client names no instant
  // of its own — the mark is the server's clock. Its answer carries the mark
  // it replaced (`previous`) and the one it set (`last_read`): Undo hands
  // both back, and the server puts the old mark back only if the chat's mark
  // is still the one this set (409 otherwise — a reply or another read moved
  // it since, and that stands).
  async _markRead(id, li = null) {
    if (!id || this._flagging) return;
    const chat0 = (this._data && this._data.chats || []).find((c) => c.id === id);
    const before = chat0 ? { unread: chat0.unread, list_group: chat0.list_group } : null;
    // A list from a server older than the field carries no count: the chat
    // is then read as it stands.
    const seen = chat0 ? chat0.inbound_seq : undefined;
    let outrun = false;
    this._flagging = true;
    try {
      const doc = await this._postRead(id, Number.isInteger(seen) ? { seen } : {});
      const chat = (this._data && this._data.chats || []).find((c) => c.id === id);
      if (chat) this._adoptRead(chat, doc, 0);
      // The count the answer left, not a zero taken for granted: a mark
      // can leave messages unread, and the shelf shows what the row does.
      const left = chat ? chat.unread : (doc && doc.unread);
      shelf.observe(`chat:${id}`, { unread: Number(left) || 0 });
      this._changed();
      // Undo needs the mark this replaced; a server that does not say it
      // (one that predates the answer's `previous`) gets no Undo offered.
      if (before && Object.prototype.hasOwnProperty.call(doc, 'previous')) {
        const undo = { restore: doc.previous == null ? null : doc.previous, marked: doc.last_read };
        offerUndo(`Marked read · ${chat ? chat.name : ''}`, () => this._undoRead(id, undo, before));
      }
    } catch (err) {
      if (li) this._swipe.slide(li, 0);
      outrun = !!err && err.message === '409';
    } finally {
      this._flagging = false;
    }
    if (outrun) this.load();
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
      /* Up to six actions in the tray: narrower buttons than the threads
         page's three, so most of a phone-wide row still shows beside them. */
      li.swipe .tray button { min-width: 48px; padding: 0 8px; font-size: .72rem; }
      .prev .pv-failed { color: #ff8a80; font-style: normal; }
      /* Multi-select: the bar over the list, and a check in place of the
         avatar on every row while selecting. */
      .sel-start { display: block; margin: 0 0 8px auto; background: transparent; border: 0;
                   color: var(--accent, #6ea8fe); font: inherit; font-size: .8rem; cursor: pointer; }
      .select-bar { position: sticky; top: 0; z-index: 4; display: flex; flex-wrap: wrap; align-items: center;
                    gap: 6px; padding: 6px 8px; margin-bottom: 8px; border-radius: 12px;
                    background: var(--card-2, #1c2230); }
      .sel-count { font-weight: 600; font-size: .85rem; margin-right: auto; }
      .sel-acts { display: flex; flex-wrap: wrap; gap: 4px; }
      .bulk { border: 0; border-radius: 8px; padding: 6px 9px; cursor: pointer; font: inherit;
              font-size: .76rem; font-weight: 600; background: var(--card, #151922); color: var(--fg, #e7ebf2); }
      .bulk:disabled { opacity: .45; cursor: default; }
      .bulk.delete { background: #d0564f; color: #fff; }
      .bulk.delete.confirm { background: #a8322c; }
      .bulk:focus-visible, .sel-start:focus-visible { outline: 2px solid var(--fg, #e7ebf2); outline-offset: 1px; }
      .sel-mark { grid-row: 1 / 3; width: 40px; height: 40px; border-radius: 50%; display: inline-flex;
                  align-items: center; justify-content: center; border: 2px solid var(--muted, #8b93a3);
                  box-sizing: border-box; color: #0b0d12; font-weight: 800; }
      .row.has-people .sel-mark { grid-row: 1 / 4; }
      .sel-mark.on { background: var(--accent, #6ea8fe); border-color: var(--accent, #6ea8fe); }
      li.swipe.selected .row { background: rgba(110, 168, 254, .14); }
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
      .prev .pv-removed { font-style: italic; }
      .prev .pv-pending { color: var(--outbound, #f0b45e); }
      /* A mail thread's participants, between its subject and the preview:
         the row grows a third line, and the avatar spans all three. */
      .row.has-people { grid-template-rows: auto auto auto; }
      .row.has-people .av { grid-row: 1 / 4; }
      .ppl { grid-column: 2; grid-row: 2; color: var(--fg, #e7ebf2); opacity: .8; font-size: .76rem;
             min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .row.has-people .prev, .row.has-people .unread, .row.has-people .mute-mark { grid-row: 3; }
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
      /* The tabs and the ＋ share a row; the menu drops from the ＋. */
      .list-head { display: flex; align-items: flex-start; gap: 8px; }
      .list-head .filter { flex: 1; min-width: 0; }
      .new-wrap { position: relative; flex: none; }
      .new-btn { width: 38px; height: 38px; border-radius: 12px; border: 0; cursor: pointer;
                 background: var(--outbound, #f0b45e); color: #0b0d12; font-size: 1.1rem; font-weight: 700;
                 display: inline-flex; align-items: center; justify-content: center; }
      .new-btn:focus-visible, .new-item:focus-visible { outline: 2px solid var(--fg, #e7ebf2); outline-offset: 2px; }
      .new-menu { position: absolute; right: 0; top: calc(100% + 6px); z-index: 5; min-width: 160px;
                  display: flex; flex-direction: column; padding: 4px; border-radius: 12px;
                  background: var(--card-2, #1c2230); border: 1px solid var(--line, rgba(231, 235, 242, .12));
                  box-shadow: 0 8px 24px rgba(0, 0, 0, .4); }
      .new-menu[hidden] { display: none; }
      .new-item { background: transparent; border: 0; border-radius: 8px; padding: 8px 10px; text-align: left;
                  color: var(--fg, #e7ebf2); cursor: pointer; font: inherit; font-size: .88rem; }
      .new-item:hover { background: rgba(110, 168, 254, .14); }
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
    // A row whose Spam waits for its Undo window is out of view.
    const all = (Array.isArray(d.chats) ? d.chats : []).filter((c) => !(this._pending && this._pending.has(c.id)));
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
    // A selected row that left the tab (a poll, a write) leaves the selection.
    if (this._selecting) {
      const here = new Set(chats.map((c) => c.id));
      for (const id of [...this._selected]) if (!here.has(id)) this._selected.delete(id);
    }
    // The tabs, and beside them the ＋ where there is something to start.
    const filter = this._filterHtml(this._full ? this._counts(d, all) : null);
    const plus = this._newHtml();
    const tabs = (plus ? `<div class="list-head">${filter}${plus}</div>` : filter) +
      this._barHtml(chats);
    // The tabs' panel: what the selected tab shows, rows or its empty line.
    const panel = (inner) => (this._full
      ? `<div role="tabpanel" id="chats-panel" aria-labelledby="chats-tab-${scope}">${inner}</div>`
      : inner);
    if (!chats.length) {
      return `${tabs}${panel(`<p class="muted">${this._emptyText(all.length > 0)}</p>`)}${this._footHtml()}`;
    }
    const shown = (this._full || isWideFrame()) ? chats : chats.slice(0, MAX_CARD_CHATS);
    this._shownIds = shown.map((c) => c.id);
    // Two accounts of one channel talking to the same peer are two chats with
    // the same name (and so is a peer's unattributed history beside its
    // account-named chat; and so is one mail thread that reached two of the
    // user's mailboxes). The name alone cannot tell them apart, so where — and
    // only where — a name repeats within a channel ACROSS accounts, each row
    // also says which account it is. Rows that share a name on one account
    // (two mail threads both called "Invoice", two contacts both called
    // "Mara") get no label: it would be the same on each and tell them apart
    // no better — the participants line and the preview do that. Computed
    // over the rows actually shown, so an unambiguous list carries none.
    const accountsOf = {};
    const nameKey = (c) => `${c.channel}\u0000${c.name}`;
    for (const c of shown) {
      const k = nameKey(c);
      (accountsOf[k] = accountsOf[k] || new Set()).add(c.account || '');
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
      // A Delete or Spam that failed: the server's reason, where the preview
      // was, until the next attempt.
      const failed = this._full && this._failed ? this._failed.get(c.id) : null;
      const prev = failed
        ? `<span class="pv-failed" role="alert">${esc((ACTIONS[failed.kind] || ACTIONS.delete).failed)}: ${esc(failed.reason)}</span>`
        : draft
        ? `<span class="pv-draft">${c.draft.author === 'agent'
          ? `Draft by ${esc(c.draft.agent || 'Ara')}` : 'Draft'}:</span> ${esc(c.draft.text)}`
        : previewHtml(c);
      // The actions sit UNDER the row, never inside it: a row is a link, and
      // a button within a link is not a button. Only on the full page — the
      // dashboard card is a glance, not a place to curate.
      const act = (label, set, title, confirm = '') =>
        `<button type="button" class="${set}" data-flag="${esc(c.id)}" data-set="${set}" ` +
        `title="${esc(title)}"${confirm ? ` data-confirm="${esc(confirm)}"` : ''}>${label}</button>`;
      // A failed Delete or Spam: its button retries, and says why it failed.
      const again = (kind, label, title, confirm = '') => {
        if (!failed || failed.kind !== kind) return act(label, kind, title, confirm);
        const off = failed.unconfigured;
        return act(off ? 'Delete not set up' : ACTIONS[kind].retry, kind,
          off ? "Chat deletion needs CHAT_ERASE_TOKEN on retinue and on the chat's gateway" : failed.reason,
          confirm);
      };
      // Deleting a mail thread does something in the mailbox too, and the
      // button says what before the second tap: the mails move to the
      // mailbox's Trash (nothing is expunged), then the chat is erased here.
      const mailbox = c.account || 'the mailbox';
      const del = isMail(c)
        ? again('delete', 'Delete',
            `Moves the thread's mails to the Trash of ${mailbox}, then erases the chat here`,
            'Move mails to Trash?')
        : again('delete', 'Delete', 'Erase this chat and all its messages from the system');
      // Spam: a mail thread's alone — its mails go to the Junk folder, and
      // the chat is muted and archived. A messenger chat is muted instead.
      const spam = isMail(c)
        ? again('spam', 'Spam',
            `Moves the thread's mails to the Junk folder of ${mailbox}, then mutes and archives the chat`)
        : '';
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
          spam + del
        : (scope === 'unread'
          ? act('Mark read', 'read', 'Mark everything in this chat as read') : '') +
          act('Archive', 'archive', 'Out of the list until the next message') +
          act('Mute', 'mute',
              'Out of the list, and the next message does not bring it back') +
          spam + del);
      // The two sides of a swipe, per tab: right marks read (Unread),
      // restores (Archived) or does nothing (Read — nothing to mark); left
      // uncovers the tray everywhere, and on Unread and Read a full swipe
      // left archives.
      const quick = { unread: 'read', archived: 'restore' }[scope] || '';
      const quickLeft = archived ? '' : 'archive';
      const QUICK_LABEL = { read: 'Mark read', restore: 'Restore' };
      const under = !this._full ? ''
        : (quick ? `<div class="quick ${quick}" aria-hidden="true">${QUICK_LABEL[quick]}</div>` : '') +
          (quickLeft ? `<div class="quick-left ${quickLeft}" aria-hidden="true">Archive</div>` : '') +
          `<div class="tray">${tray}</div>`;
      const people = participantsText(c);
      const picked = this._selecting && this._selected.has(c.id);
      return (this._full
        ? `<li class="swipe${failed ? ' failed' : ''}${picked ? ' selected' : ''}" data-id="${esc(c.id)}"` +
          (quick ? ` data-quick="${quick}"` : '') +
          (quickLeft ? ` data-quick-left="${quickLeft}"` : '') + '>'
        : '<li>') + under +
        `<a class="row${unread ? ' has-unread' : ''}${people ? ' has-people' : ''}"` +
        (this._full ? ' draggable="false" ' : ' ') +
        (this._selecting ? `aria-pressed="${picked}" ` : '') +
        `href="/chat.html?id=${encodeURIComponent(c.id)}">` +
        (this._selecting
          ? `<span class="sel-mark${picked ? ' on' : ''}" aria-hidden="true">${picked ? '&#10003;' : ''}</span>`
          : `${avatarHtml(c)}`) +
        `<span class="name">${c.starred ? STAR_MARK : ''}${esc(c.name)}` +
        (accountsOf[nameKey(c)].size > 1
          ? `<span class="acct" title="${c.account
              ? `On the account ${esc(c.account)}`
              : 'Older messages, from before the account was recorded'}">${
              c.account ? esc(c.account) : 'earlier'}</span>`
          : '') +
        `</span>` +
        `<span class="when">${esc(fmtAge((c.last || {}).ts))}</span>` +
        (people ? `<span class="ppl" title="${esc(people)}">${esc(people)}</span>` : '') +
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
