// Chats card: the messenger mirror's home screen — every channel conversation
// (Signal / WhatsApp / Telegram, one peer or group each) as one row, ordered by
// last activity, with unread badge, channel mark and last-message preview. Rows
// link to the chat's own page (chat.html), which renders the full mirror beside
// its companion thread. With the `full` attribute (chats.html) the list drops
// the dashboard cap and adds an Active/Archived filter, like the conversations
// page. Archived chats are excluded everywhere else; `muted` only changes the
// badge treatment here (its real meaning — no Web Push, and no un-archive on a
// new inbound message — is the server's, once the live chat API exists).
//
// FIXTURE DATA for now: the card reads /data/chats.json, a static document
// shaped exactly like the future `GET /chats` response (the messenger-chats
// design, phase 2). The document shape is the API contract draft — see
// webapp/README.md, "Messenger chats (fixture)". Swapping fixture for live API
// is a `src` change only; nothing in here knows it is reading a file.

import { RetinueCard, esc, fmtAge, isWideFrame, onFrameChange } from './base.js';

// Rows shown on the dashboard card before "All chats →" takes over — the same
// cap logic as the conversations card: only the phone layout, where each row
// lengthens the page, caps the list.
const MAX_CARD_CHATS = 5;

// Channel marks: no brand assets in the shell, so a lettered dot in the
// channel's recognisable colour does the telling.
export const CHANNELS = {
  signal: { label: 'Signal', mark: 'S', color: '#3a76f0' },
  whatsapp: { label: 'WhatsApp', mark: 'W', color: '#25d366' },
  telegram: { label: 'Telegram', mark: 'T', color: '#2aabee' },
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
export function avatarHtml(chat) {
  return `<span class="av" style="background:${colorFor(chat.id)}" aria-hidden="true">` +
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
    // Crossing the layout breakpoint changes how many rows fit (cap vs all).
    this._offFrame = onFrameChange(() => {
      if (this._data) this.renderState({ state: 'ok', data: this._data });
    });
    super.connectedCallback();
  }

  disconnectedCallback() {
    if (this._offFrame) this._offFrame();
    this._offFrame = null;
  }

  // RetinueCard renders static content; the full page's scope filter is the
  // one interactive control, wired after each render.
  renderState(s) {
    super.renderState(s);
    this.shadowRoot.querySelectorAll('[data-scope]').forEach((el) =>
      el.addEventListener('click', () => {
        const scope = el.getAttribute('data-scope');
        if (scope === this._scope) return;
        this._scope = scope;
        if (this._data) this.renderState({ state: 'ok', data: this._data });
      }));
  }

  css() {
    return `
      ul.list { gap: 4px; }
      li { margin: 0; }
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
            border: 2px solid var(--card, #151922); box-sizing: content-box; }
      .name { font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; }
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
    // Archived filter is where they remain reachable.
    const archived = this._full && this._scope === 'archived';
    const chats = all.filter((c) => !!c.archived === archived);
    if (!chats.length) {
      const msg = archived ? 'No archived chats.' : 'No chats yet.';
      return `${this._filterHtml()}<p class="muted">${msg}</p>`;
    }
    const shown = (this._full || isWideFrame()) ? chats : chats.slice(0, MAX_CARD_CHATS);
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
      return `<li><a class="row${c.unread ? ' has-unread' : ''}" ` +
        `href="/chat.html?id=${encodeURIComponent(c.id)}">` +
        `${avatarHtml(c)}` +
        `<span class="name">${esc(c.name)}</span>` +
        `<span class="when">${esc(fmtAge((c.last || {}).ts))}</span>` +
        `<span class="prev">${prev}</span>` +
        badge +
        `</a></li>`;
    }).join('');
    const foot = this._full ? ''
      : `<div class="foot"><a class="all-link" href="/chats.html">All chats &#8594;</a></div>`;
    return `${this._filterHtml()}<ul class="list">${rows}</ul>${foot}`;
  }
}

customElements.define('retinue-chats', RetinueChats);
