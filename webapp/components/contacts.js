// The address book: every person Retinue knows, whatever channel reaches them
// (docs/contacts.md). A contact is one file in one chamber; this page lists
// them, finds them by name or address, and edits what a person carries:
//
//   - their handles — e-mail addresses, phone numbers, Signal / WhatsApp /
//     Telegram accounts (or any other channel by name);
//   - the e-mail domains they write from — an organisation, a shop that sends
//     receipts: `example.org` for every address there, `*.example.org` for
//     that domain and everything below it. A domain is not a handle (nothing
//     is sent to it); it says whose mail a mail is when nobody holds the
//     sender's address;
//   - the attention model's facts about them — the sphere they belong to and
//     further ones, their importance prior, the Focus modes they may
//     interrupt;
//   - the VIP flag: their messages ring at once and a model works them the
//     moment they arrive, on every channel, e-mail included;
//   - their instruction: the user's standing brief a model works each of
//     their messages by, mail included — it decides what is done with a
//     message and how loudly it rings. One can be copied from another person
//     (a team).
//
// A person's handles lead to their conversations. A tap on a messenger handle
// (WhatsApp, Signal, Telegram, a phone for SMS) opens the chat with them on
// that channel — or, where there is none yet, a new one, the first message of
// which starts it; where several accounts or chats would do, a short list
// asks which. A tap on an e-mail address always asks: each mail thread the
// address is in, newest first, and *New e-mail* (the compose sheet, addressed
// to them). The server says what there is (GET /contacts/chats).
//
// Live endpoints, never cached by the service worker:
//   GET  /contacts[?q=]         -> { locations, default, contacts }
//   GET  /contacts/chats?channel=&handle=&person=
//                               -> { handle, chats, new } for one handle
//   POST /contacts              -> create (needs `chamber`; `domains`)
//   POST /contacts/<id>         -> change (`add_domains` / `remove_domains`)
//   GET  /attention/profile     -> { focus: { spheres, modes } } for the choices
//
// A new contact always names the chamber it is kept in; the address book's
// default — the chamber the last contact was created in — is pre-selected.

import { esc } from './base.js';

const CHANNELS = ['email', 'sms', 'signal', 'whatsapp', 'telegram'];
const CHANNEL_LABEL = { email: 'E-mail', sms: 'Phone', signal: 'Signal', whatsapp: 'WhatsApp', telegram: 'Telegram' };
const IMPORTANCE = [
  [null, 'no prior'], [0, '0 · ignore'], [1, '1 · low'], [2, '2'], [3, '3'], [4, '4 · high'], [5, '5 · top'],
];

const CSS = `
  :host { display: block; }
  * { box-sizing: border-box; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 12px 0 10px; }
  h2 { font-size: .82rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
       color: var(--muted, #8b93a3); margin: 0; }
  .muted, .note { color: var(--muted, #8b93a3); font-size: .78rem; }
  .err { color: #ff8a8a; font-size: .85rem; margin: 8px 0; }
  input[type=search], input[type=text], input[type=email], select, textarea {
    font: inherit; color: var(--fg, #e7ebf2); background: var(--card-2, #1c2230);
    border: 1px solid var(--line, rgba(231, 235, 242, .12)); border-radius: 10px; padding: 8px 10px; min-width: 0; }
  /* A select is as wide as its longest option (a contact's name in "Copy
     instruction from…"): never wider than the card. */
  select { max-width: 100%; }
  input[type=search] { width: 100%; margin-bottom: 10px; }
  .btn { font: inherit; font-size: .84rem; color: var(--fg, #e7ebf2); background: var(--card-2, #1c2230);
         border: 1px solid var(--line, rgba(231, 235, 242, .12)); border-radius: 999px; padding: 6px 12px; cursor: pointer; }
  .btn.primary { background: var(--accent, #6ea8fe); color: #0b0d12; border-color: transparent; font-weight: 600; }
  .btn.tiny { font-size: .76rem; padding: 3px 9px; }
  .btn.on { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }
  .btn[disabled] { opacity: .5; cursor: default; }
  ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px;
       grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); align-content: start; }
  li { min-width: 0; }
  li > button.row { all: unset; display: block; width: 100%; box-sizing: border-box; cursor: pointer;
       padding: 10px 12px; border-radius: 12px; background: var(--card-2, #1c2230);
       -webkit-tap-highlight-color: transparent; }
  @media (hover: hover) { li > button.row:hover { outline: 1px solid var(--accent, #6ea8fe); } }
  .name { color: var(--fg, #e7ebf2); font-weight: 600; }
  .vip { display: inline-block; font-size: .66rem; font-weight: 700; letter-spacing: .06em;
         color: #0b0d12; background: #f5c542; border-radius: 6px; padding: 1px 5px; margin-left: 6px; vertical-align: 2px; }
  .brief { display: inline-block; font-size: .66rem; font-weight: 700; letter-spacing: .06em;
           color: var(--accent, #6ea8fe); border: 1px solid currentColor; border-radius: 6px; padding: 0 5px;
           margin-left: 6px; vertical-align: 2px; }
  textarea[data-set="instruction"] { width: 100%; min-height: 4.5em; resize: vertical; line-height: 1.35; }
  .meta { font-size: .76rem; color: var(--muted, #8b93a3); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .editor { background: var(--card, #151922); border: 1px solid var(--accent, #6ea8fe); border-radius: 14px;
            padding: 12px; display: grid; gap: 12px;
            /* One column the card's width: a long handle, name or option wraps
               inside it instead of widening the page past the screen. */
            grid-template-columns: minmax(0, 1fr); }
  .editor > * { min-width: 0; }
  .editor .k { font-size: .7rem; font-weight: 600; letter-spacing: .05em; text-transform: uppercase;
               color: var(--muted, #8b93a3); margin-bottom: 5px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .handle { display: flex; align-items: center; gap: 6px; font-size: .86rem; }
  .handle > span { min-width: 0; overflow-wrap: anywhere; }
  .handle .ch { flex: none; }
  .handle .ch { color: var(--muted, #8b93a3); min-width: 72px; }
  .handle .covers { color: var(--muted, #8b93a3); font-size: .76rem; }
  /* A domain row: the domain and its ✕ on the first line, who it covers
     below, aligned under the domain. */
  .handle.domain { flex-wrap: wrap; row-gap: 0; }
  .handle.domain .covers { order: 1; flex-basis: 100%; padding-left: 78px; }
  .handle .x { all: unset; cursor: pointer; color: var(--muted, #8b93a3); padding: 0 6px; }
  .add { display: flex; gap: 6px; flex-wrap: wrap; }
  .add input { flex: 1 1 160px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  input[data-set="name"] { width: 100%; font-weight: 600; }
  /* A row is a card: the button opens the editor, the handles under it each
     lead to their conversation. */
  li.card { padding: 0; border-radius: 12px; background: var(--card-2, #1c2230); }
  @media (hover: hover) { li.card:hover { outline: 1px solid var(--accent, #6ea8fe); } }
  li.card > button.row { background: none; padding-bottom: 4px; }
  @media (hover: hover) { li.card > button.row:hover { outline: none; } }
  .reach { display: flex; flex-wrap: wrap; gap: 2px 10px; padding: 0 12px 10px; font-size: .76rem;
           color: var(--muted, #8b93a3); min-width: 0; }
  .reach > span { overflow-wrap: anywhere; }
  button.go { all: unset; cursor: pointer; color: var(--accent, #6ea8fe); overflow-wrap: anywhere;
              -webkit-tap-highlight-color: transparent; }
  button.go:focus-visible { outline: 2px solid var(--accent, #6ea8fe); outline-offset: 2px; border-radius: 4px; }
  button.go .via { color: var(--muted, #8b93a3); }
  .handle button.go { font-size: .86rem; }
  /* What a tap on a handle can open, when there is more than one thing. */
  .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, .5); z-index: 50;
           display: flex; align-items: flex-end; justify-content: center; }
  .pick { width: min(100%, 520px); max-height: 80vh; overflow: auto; background: var(--card, #151922);
          border: 1px solid var(--line, rgba(231, 235, 242, .12)); border-radius: 16px 16px 0 0;
          padding: 14px 16px calc(14px + env(safe-area-inset-bottom, 0px)); display: grid; gap: 8px; }
  @media (min-width: 640px) { .scrim { align-items: center; } .pick { border-radius: 16px; } }
  .pick h3 { margin: 0; font-size: .95rem; overflow-wrap: anywhere; }
  .pick .opt { all: unset; display: block; box-sizing: border-box; cursor: pointer; padding: 10px 12px;
               border-radius: 12px; background: var(--card-2, #1c2230); }
  .pick .opt:focus-visible { outline: 2px solid var(--accent, #6ea8fe); }
  @media (hover: hover) { .pick .opt:hover { outline: 1px solid var(--accent, #6ea8fe); } }
  .pick .opt.new { color: var(--accent, #6ea8fe); font-weight: 600; }
  .pick .opt .meta { white-space: normal; }
`;

function sphereList(p) {
  return [p.sphere, ...(p.tags || [])].filter(Boolean);
}

// What the user typed, as the address book files a domain: lowercased, with
// the delivery gate's spelling of the same two shapes (`*@example.org`,
// `*@*.example.org`) and a leading `@` read as what they mean. Only the two
// mistakes worth catching before the round trip are refused here — an
// address, and something that is no domain at a glance. The server is the
// authority on the rest (a public suffix, a domain somebody else has), and
// its refusal is shown as it words it (docs/contacts.md, "E-mail domains").
export function domainSpec(raw) {
  let spec = String(raw == null ? '' : raw).trim().toLowerCase();
  if (spec.startsWith('*@')) spec = spec.slice(2);
  else if (spec.startsWith('@')) spec = spec.slice(1);
  if (!spec) return { spec: '' };
  if (spec.includes('@')) {
    return { error: 'That is an address, not a domain — add it under “Reached by”.' };
  }
  const base = spec.startsWith('*.') ? spec.slice(2) : spec;
  if (!base.includes('.') || base.includes('*') || /\s/.test(base)) {
    return { error: 'A domain looks like example.org — or *.example.org for it and everything below it.' };
  }
  return { spec };
}

// In words, who a filed domain covers.
export function domainCovers(spec) {
  return spec.startsWith('*.')
    ? `${spec.slice(2)} and every domain below it`
    : `every address @${spec}`;
}

// The channels a tap on a handle leads to a conversation on: the ones that
// have chats (an SMS sender id is no number and cannot be written to, which
// the server says when asked).
const CHATTABLE = new Set(CHANNELS);

// A chat's last message time, short: today's as a time, older ones as a date.
function when(ts) {
  const d = new Date(ts);
  if (!ts || Number.isNaN(d.getTime())) return '';
  const now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
}

// What a tap on a handle does, from GET /contacts/chats: go straight to the
// one place it can only mean, or ask. → {go: chat id} | {compose: offer} |
// {ask: true} | {none: sentence}.
export function handleTarget(answer, channel) {
  const chats = Array.isArray(answer && answer.chats) ? answer.chats.filter((c) => c && c.id) : [];
  const fresh = Array.isArray(answer && answer.new) ? answer.new.filter((n) => n && (n.id || n.compose)) : [];
  if (channel === 'email') {
    if (chats.length) return { ask: true };
    const compose = fresh.find((n) => n.compose);
    return compose ? { compose } : { none: 'No mailbox can send a new e-mail from here right now.' };
  }
  if (chats.length === 1) return { go: chats[0].id };
  if (chats.length > 1) return { ask: true };
  if (fresh.length === 1) return { go: fresh[0].id };
  if (fresh.length > 1) return { ask: true };
  const label = CHANNEL_LABEL[channel] || channel;
  return { none: `There is no chat on ${label} with them yet, and none can be started from here: ` +
    `no ${label} account is set up for chats, or this handle is not one a message can be sent to.` };
}

export function chatUrl(id) {
  return `/chat.html?id=${encodeURIComponent(id)}`;
}

// Whom a link asks the page to open (chats.js contactHref): `{contact}` for
// an id, `{channel, handle}` for whoever holds a handle, or null. A channel
// the page does not know is no handle it can file, so it is no ask.
export function wantedFromQuery(search) {
  const p = new URLSearchParams(String(search || ''));
  const contact = (p.get('contact') || '').trim();
  if (contact) return { contact };
  const channel = (p.get('channel') || '').trim();
  const handle = (p.get('handle') || '').trim();
  return handle && CHANNELS.includes(channel) ? { channel, handle } : null;
}

// Whether a person holds a handle: an address in any case, a number in any
// spelling of its digits — on the asked channel, or (a number) on any, as
// the address book's own `phones` say one person is reached on several.
export function holdsHandle(person, channel, handle) {
  const digits = (s) => String(s || '').replace(/[^\d+]/g, '');
  const want = String(handle || '').trim();
  if (!want) return false;
  if (channel === 'email') {
    return (person.handles || []).some((h) => h.channel === 'email' && h.handle.toLowerCase() === want.toLowerCase());
  }
  if ((person.handles || []).some((h) => h.channel === channel && h.handle === want)) return true;
  const n = digits(want);
  return n.length > 4 && ((person.phones || []).concat((person.handles || [])
    .filter((h) => h.channel !== 'email').map((h) => h.handle))).some((x) => digits(x) === n);
}

class RetinueContacts extends HTMLElement {
  constructor() {
    super();
    this._data = null;      // GET /contacts
    this._focus = null;     // spheres + modes, for the choices
    this._q = '';
    this._open = null;      // id of the contact being edited, or 'new'
    this._form = null;
    this._busy = false;
    this._error = '';
    // A tap on a handle: {person, name, channel, handle, busy, answer, error}
    // while it is being looked up or its choices are shown.
    this._pick = null;
    // Whom a link from a chat's avatar asks to open, read once per page load.
    this._want = wantedFromQuery((globalThis.location && globalThis.location.search) || '');
  }

  connectedCallback() {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.addEventListener('click', (e) => this._onClick(e));
      this.shadowRoot.addEventListener('input', (e) => this._onInput(e));
      this.shadowRoot.addEventListener('change', (e) => this._onInput(e));
      this.shadowRoot.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this._pick) { e.preventDefault(); this._closePick(); }
      });
    }
    this.render();
    this.load();
  }

  // Open the person a link asked for, once, on the first address book that
  // arrives: their editor, scrolled into view. A handle nobody holds starts a
  // new contact with it filled in; an id the book no longer has says so.
  _openWanted() {
    const want = this._want;
    if (!want || !this._data) return;
    this._want = null;
    const list = this._data.contacts || [];
    let hit = null;
    if (want.contact) hit = list.find((c) => c.id === want.contact) || null;
    else hit = list.find((c) => holdsHandle(c, want.channel, want.handle)) || null;
    if (hit) {
      this._startEdit(hit.id);
    } else if (want.handle) {
      this._startEdit('new');
      this._form.handles.push({ channel: want.channel, handle: want.handle });
      this.render();
    } else {
      this._error = 'That contact is no longer in the address book.';
      this.render();
      return;
    }
    const ed = this.shadowRoot.querySelector('.editor');
    if (ed && ed.scrollIntoView) ed.scrollIntoView({ block: 'start' });
  }

  async load() {
    try {
      const [book, prof] = await Promise.all([
        fetch(this._q ? `/contacts?q=${encodeURIComponent(this._q)}` : '/contacts', { cache: 'no-store' }),
        this._focus ? null : fetch('/attention/profile', { cache: 'no-store' }),
      ]);
      if (!book.ok) throw new Error(`HTTP ${book.status}`);
      this._data = await book.json();
      if (prof && prof.ok) this._focus = (await prof.json()).focus || {};
      this._error = '';
    } catch (err) {
      this._error = `Could not load the address book (${String((err && err.message) || err)}).`;
    }
    this.render();
    this._openWanted();
  }

  _contact(id) {
    return ((this._data && this._data.contacts) || []).find((c) => c.id === id) || null;
  }

  _startEdit(id) {
    const c = id === 'new' ? null : this._contact(id);
    this._open = id;
    this._error = '';
    this._form = c
      ? { name: c.name, sphere: c.sphere || '', tags: [...(c.tags || [])], importance: c.importance,
          permits: [...(c.permits || [])], vip: Boolean(c.vip), instruction: c.instruction || '',
          handles: c.handles.map((h) => ({ ...h })), domains: [...(c.domains || [])], chamber: c.chamber,
          add: { channel: 'email', handle: '', domain: '' } }
      : { name: '', sphere: '', tags: [], importance: null, permits: [], vip: false, instruction: '', handles: [],
          domains: [], chamber: (this._data && this._data.default) || '',
          add: { channel: 'email', handle: '', domain: '' } };
    this.render();
    const input = this.shadowRoot.querySelector('[data-set="name"]');
    if (input && id === 'new') input.focus();
  }

  _onInput(e) {
    const el = e.target.closest('[data-set]');
    if (!el) return;
    const what = el.getAttribute('data-set');
    if (what === 'q') {
      this._q = el.value.trim();
      clearTimeout(this._qTimer);
      this._qTimer = setTimeout(() => this.load(), 200);
      return;
    }
    if (!this._form) return;
    if (what === 'name') this._form.name = el.value;
    else if (what === 'add-handle') this._form.add.handle = el.value;
    else if (what === 'add-channel') this._form.add.channel = el.value;
    else if (what === 'add-domain') this._form.add.domain = el.value;
    else if (what === 'chamber') this._form.chamber = el.value;
    else if (what === 'instruction') this._form.instruction = el.value;
    else if (what === 'instruction-from' && e.type === 'change') {
      // Copy another person's instruction, to edit here or keep as it is.
      const from = this._contact(el.value);
      if (from && from.instruction) { this._form.instruction = from.instruction; this.render(); }
    }
    else if (what === 'importance' && e.type === 'change') {
      this._form.importance = el.value === '' ? null : Number(el.value);
    }
  }

  _onClick(e) {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.getAttribute('data-act');
    // The choices of a handle: a press on the scrim itself (not the sheet on
    // it) shuts them.
    if (act === 'pick-close') { if (e.target === el) this._closePick(); return; }
    if (act === 'pick-dismiss') { this._closePick(); return; }
    if (act === 'pick-chat') { this._leave(chatUrl(el.getAttribute('data-id'))); return; }
    if (act === 'pick-compose') { this._compose(); return; }
    if (act === 'go') {
      this._go(el.getAttribute('data-id'), el.getAttribute('data-ch'), el.getAttribute('data-h'));
      return;
    }
    if (this._busy) return;
    const f = this._form;
    const arg = el.getAttribute('data-v');
    switch (act) {
      case 'open': this._startEdit(el.getAttribute('data-id')); break;
      case 'new': this._startEdit('new'); break;
      case 'cancel': this._open = null; this._form = null; this._error = ''; this.render(); break;
      case 'sphere': if (f) { f.sphere = f.sphere === arg ? '' : arg; f.tags = f.tags.filter((t) => t !== f.sphere); this.render(); } break;
      case 'tag': if (f) { f.tags = f.tags.includes(arg) ? f.tags.filter((t) => t !== arg) : f.tags.concat([arg]); this.render(); } break;
      case 'permit': if (f) { f.permits = f.permits.includes(arg) ? f.permits.filter((m) => m !== arg) : f.permits.concat([arg]); this.render(); } break;
      case 'vip': if (f) { f.vip = !f.vip; this.render(); } break;
      case 'drop-handle': if (f) { f.handles.splice(Number(arg), 1); this.render(); } break;
      case 'add-handle': this._addHandle(); break;
      case 'drop-domain': if (f) { f.domains.splice(Number(arg), 1); this.render(); } break;
      case 'add-domain': this._addDomain(); break;
      case 'save': this._save(); break;
      default: break;
    }
  }

  // A handle tapped: ask the server what it leads to, then go — or show the
  // choices.
  async _go(person, channel, handle) {
    if (!channel || !handle || (this._pick && this._pick.busy)) return;
    const c = this._contact(person);
    const pick = { person, name: (c && c.name) || '', channel, handle, busy: true, answer: null, error: '' };
    this._pick = pick;
    this.render();
    try {
      const q = new URLSearchParams({ channel, handle });
      if (person) q.set('person', person);
      const res = await fetch(`/contacts/chats?${q}`, { cache: 'no-store' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      if (this._pick !== pick) return;
      pick.answer = data;
      pick.busy = false;
      const t = handleTarget(data, channel);
      if (t.go) { this._leave(chatUrl(t.go)); return; }
      if (t.compose) { this._compose(); return; }
      if (t.none) pick.error = t.none;
    } catch (err) {
      if (this._pick !== pick) return;
      pick.busy = false;
      pick.error = `Could not look up their chats (${String((err && err.message) || err)}).`;
    }
    this.render();
  }

  _closePick() {
    if (!this._pick) return;
    this._pick = null;
    this.render();
  }

  _leave(url) {
    this._pick = null;
    this.render();
    window.location.href = url;
  }

  // A new mail to the tapped address: the compose sheet, From the mailboxes
  // the server named and To the person. Loaded only when asked for.
  async _compose() {
    const p = this._pick;
    const offer = p && p.answer && (p.answer.new || []).find((n) => n && n.compose);
    if (!offer) return;
    const handle = (p.answer.handle && p.answer.handle.handle) || p.handle;
    this._pick = null;
    this.render();
    const { openComposeSheet } = await import('./compose-sheet.js');
    openComposeSheet({ channel: 'email', label: 'E-mail', accounts: offer.accounts || [],
      to: { address: handle, name: p.name } });
  }

  // A handle as a link to its conversation, or as text where it leads none.
  _handleLink(person, h, withChannel) {
    const text = withChannel ? `${CHANNEL_LABEL[h.channel] || h.channel} ${h.handle}` : h.handle;
    if (!person || !CHATTABLE.has(h.channel)) return `<span>${esc(text)}</span>`;
    const label = h.channel === 'email' ? `Mail threads with ${h.handle}` : `Chat with ${h.handle} on ${CHANNEL_LABEL[h.channel] || h.channel}`;
    return `<button type="button" class="go" data-act="go" data-id="${esc(person)}" data-ch="${esc(h.channel)}" ` +
      `data-h="${esc(h.handle)}" aria-label="${esc(label)}">` +
      (withChannel ? `<span class="via">${esc(CHANNEL_LABEL[h.channel] || h.channel)}</span> ` : '') +
      `${esc(h.handle)}</button>`;
  }

  // The choices for a handle: its chats, and a new one.
  _pickHtml() {
    const p = this._pick;
    if (!p) return '';
    const label = CHANNEL_LABEL[p.channel] || p.channel;
    const mail = p.channel === 'email';
    let body;
    if (p.busy) body = '<p class="muted">Looking up their chats…</p>';
    else if (p.error) body = `<div class="err">${esc(p.error)}</div>`;
    else {
      const a = p.answer || {};
      const several = new Set((a.chats || []).map((c) => c.account).filter(Boolean)).size > 1
        || (a.new || []).filter((n) => n.id).length > 1;
      const chats = (a.chats || []).map((c) =>
        `<button type="button" class="opt" data-act="pick-chat" data-id="${esc(c.id)}">` +
        `<div class="name">${esc(c.name || p.handle)}</div>` +
        `<div class="meta">${[c.group ? 'group' : '', several && c.account ? `as ${c.account}` : '',
          c.list_group === 'archived' ? 'archived' : '', when(c.ts)].filter(Boolean).map(esc).join(' · ')}</div>` +
        `</button>`).join('');
      const fresh = (a.new || []).map((n) => (n.compose
        ? `<button type="button" class="opt new" data-act="pick-compose">New e-mail</button>`
        : `<button type="button" class="opt new" data-act="pick-chat" data-id="${esc(n.id)}">` +
          `New ${esc(label)} chat${several ? ` <span class="meta">as ${esc(n.account)}</span>` : ''}</button>`));
      // A messenger chat that exists already is the one to write in: a new
      // one is offered only from an account that has none with them.
      const have = new Set((a.chats || []).map((c) => c.account));
      const offered = mail ? fresh : fresh.filter((_, i) => !have.has(a.new[i].account));
      body = chats + offered.join('') || '<p class="muted">Nothing to open.</p>';
    }
    const title = mail ? `Mail with ${p.handle}` : `${label} · ${p.handle}`;
    return `<div class="scrim" data-act="pick-close">` +
      `<div class="pick" role="dialog" aria-modal="true" aria-label="${esc(title)}">` +
      `<h3>${esc(title)}</h3>${body}` +
      `<div class="actions"><button type="button" class="btn" data-act="pick-dismiss">Close</button></div>` +
      `</div></div>`;
  }

  _addHandle() {
    const f = this._form;
    const handle = (f.add.handle || '').trim();
    if (!handle) return;
    if (!f.handles.some((h) => h.channel === f.add.channel && h.handle === handle)) {
      f.handles.push({ channel: f.add.channel, handle });
    }
    f.add.handle = '';
    this.render();
  }

  // Take the domain field into the form. False when what is in the field is
  // no domain: the field keeps it and the reason is shown, so nothing typed
  // is dropped on the way to a save.
  _addDomain() {
    const f = this._form;
    const got = domainSpec(f.add.domain);
    if (got.error) { this._error = got.error; this.render(); return false; }
    if (got.spec && !f.domains.includes(got.spec)) f.domains.push(got.spec);
    f.add.domain = '';
    this._error = '';
    this.render();
    return true;
  }

  async _save() {
    const f = this._form;
    if (!f) return;
    if ((f.add.handle || '').trim()) this._addHandle();
    if ((f.add.domain || '').trim() && !this._addDomain()) return;
    if (!f.name.trim()) { this._error = 'A name, so the person can be called something.'; this.render(); return; }
    const isNew = this._open === 'new';
    if (isNew && !f.chamber) { this._error = 'A chamber to keep the contact in.'; this.render(); return; }
    const facts = { name: f.name.trim(), sphere: f.sphere || null, tags: f.tags, importance: f.importance,
      permits: f.permits, vip: f.vip, instruction: (f.instruction || '').trim() };
    let body;
    if (isNew) {
      body = { ...facts, chamber: f.chamber, handles: f.handles };
      if (f.domains.length) body.domains = f.domains;
    } else {
      const was = this._contact(this._open) || { handles: [] };
      const key = (h) => `${h.channel}:${h.handle}`;
      const now = new Set(f.handles.map(key));
      const before = new Set(was.handles.map(key));
      body = { ...facts,
        add: f.handles.filter((h) => !before.has(key(h))),
        remove: was.handles.filter((h) => !now.has(key(h))) };
      // Domains travel as a difference too, and only when there is one: what
      // this form did not touch is not restated, so a domain filed elsewhere
      // meanwhile (the CLI, another device) is not undone by this save.
      const had = was.domains || [];
      const addDomains = f.domains.filter((d) => !had.includes(d));
      const removeDomains = had.filter((d) => !f.domains.includes(d));
      if (addDomains.length) body.add_domains = addDomains;
      if (removeDomains.length) body.remove_domains = removeDomains;
    }
    this._busy = true;
    this.render();
    try {
      const res = await fetch(isNew ? '/contacts' : `/contacts/${encodeURIComponent(this._open)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      this._open = null;
      this._form = null;
      this._error = '';
      await this.load();
    } catch (err) {
      this._error = `Could not save (${String((err && err.message) || err)}).`;
    } finally {
      this._busy = false;
      this.render();
    }
  }

  _row(c) {
    // A domain reads as "anyone at": @example.org, @*.example.org. Each
    // handle leads to the conversation with them on its channel; the row
    // itself opens the editor.
    const reach = (c.handles || []).map((h) => this._handleLink(c.id, h, true))
      .concat((c.domains || []).map((d) => `<span>${esc(`@${d}`)}</span>`));
    const spheres = sphereList(c);
    return `<li class="card"><button class="row" data-act="open" data-id="${esc(c.id)}">` +
      `<div class="name">${esc(c.name)}${c.vip ? '<span class="vip">VIP</span>' : ''}` +
      `${c.instruction ? '<span class="brief" title="Their messages are worked by your instruction">BRIEF</span>' : ''}</div>` +
      `<div class="meta">${spheres.length ? esc(spheres.join(', ')) + ' · ' : ''}${esc(c.chamber)}</div>` +
      `</button><div class="reach">${reach.join('') || '<span>no handles</span>'}</div></li>`;
  }

  _editor() {
    const f = this._form;
    const busy = this._busy ? ' disabled' : '';
    const isNew = this._open === 'new';
    const focus = this._focus || {};
    const spheres = (focus.spheres || []).filter((s) => s !== 'unknown');
    const modes = Object.entries(focus.modes || {});
    const chambers = ((this._data && this._data.locations) || []).map((l) => l.chamber);
    const chip = (act, v, on, label) =>
      `<button class="btn tiny${on ? ' on' : ''}" data-act="${act}" data-v="${esc(v)}"${busy}>${on && act !== 'sphere' ? '✓ ' : ''}${esc(label || v)}</button>`;
    const where = isNew
      ? `<div><div class="k">Kept in</div>${chambers.length
        ? `<select data-set="chamber"${busy}>${chambers.map((c) =>
          `<option value="${esc(c)}"${f.chamber === c ? ' selected' : ''}>${esc(c)}</option>`).join('')}</select>`
        : '<span class="note">No chamber keeps contacts — each opts out in chambers.json, or none is mounted.</span>'}</div>`
      : `<div class="note">Kept in ${esc(f.chamber)}.</div>`;
    // A handle the contact has on file leads to its conversation; one added
    // in this form is not saved yet, so there is nothing to look up.
    const saved = new Set(((!isNew && this._contact(this._open)) || { handles: [] }).handles
      .map((h) => `${h.channel}:${h.handle}`));
    const handles = f.handles.map((h, i) =>
      `<div class="handle"><span class="ch">${esc(CHANNEL_LABEL[h.channel] || h.channel)}</span>` +
      this._handleLink(saved.has(`${h.channel}:${h.handle}`) ? this._open : null, h, false) +
      `<button class="x" data-act="drop-handle" data-v="${i}" aria-label="Remove">✕</button></div>`).join('');
    const domains = f.domains.map((d, i) =>
      `<div class="handle domain"><span class="ch">Domain</span><span>${esc(d)}</span>` +
      `<span class="covers">${esc(domainCovers(d))}</span>` +
      `<button class="x" data-act="drop-domain" data-v="${i}" aria-label="Remove the domain ${esc(d)}">✕</button></div>`).join('');
    return `<div class="editor">` +
      `<input type="text" data-set="name" value="${esc(f.name)}" placeholder="Their name" autocomplete="off">` +
      where +
      `<div><div class="k">Reached by</div>${handles || '<div class="note">No handle yet.</div>'}` +
      `<div class="add" style="margin-top:6px"><select data-set="add-channel">` +
      CHANNELS.map((c) => `<option value="${c}"${f.add.channel === c ? ' selected' : ''}>${CHANNEL_LABEL[c]}</option>`).join('') +
      `</select><input type="text" data-set="add-handle" value="${esc(f.add.handle)}" placeholder="address, +41 …, @name" autocomplete="off">` +
      `<button class="btn tiny" data-act="add-handle"${busy}>Add</button></div></div>` +
      `<div><div class="k">Writes e-mail from</div>${domains || '<div class="note">No domain. Most people need none: their addresses above are enough.</div>'}` +
      `<div class="add" style="margin-top:6px"><input type="text" data-set="add-domain" value="${esc(f.add.domain)}" ` +
      `placeholder="example.org, or *.example.org" aria-label="An e-mail domain they write from" autocomplete="off" autocapitalize="off" spellcheck="false">` +
      `<button class="btn tiny" data-act="add-domain"${busy}>Add</button></div>` +
      `<div class="note">For an organisation that writes from many addresses: a mail from anyone at the domain is theirs, ` +
      `unless a person of their own holds that address — so VIP and the instruction below apply to every sender there. ` +
      `A mail's sender address is the sender's own claim; file people at a freemail provider by address, never by its domain.</div></div>` +
      `<div><div class="k">Belongs to</div><div class="chips">${spheres.map((s) => chip('sphere', s, f.sphere === s)).join('')}</div>` +
      `<div class="note">The sphere decides which modes let them through.</div></div>` +
      `<div><div class="k">Also</div><div class="chips">${spheres.filter((s) => s !== f.sphere).map((s) => chip('tag', s, f.tags.includes(s))).join('')}</div></div>` +
      `<div><div class="k">Importance</div><select data-set="importance"${busy}>` +
      IMPORTANCE.map(([v, l]) => `<option value="${v == null ? '' : v}"${(f.importance == null ? v == null : Number(f.importance) === v) ? ' selected' : ''}>${l}</option>`).join('') +
      `</select> <span class="note">the prior for a message of theirs; the sheet's − / + teach it too</span></div>` +
      `<div><div class="k">May interrupt in</div><div class="chips">${modes.map(([id, m]) => chip('permit', id, f.permits.includes(id), (m && m.name) || id)).join('')}</div></div>` +
      `<div><div class="chips">${chip('vip', 'vip', f.vip, 'VIP')}</div>` +
      `<div class="note">A VIP's messages ring at once, and a model works them the moment they arrive — on every channel above, ` +
      `e-mail and its domains included. Mail from anyone who is neither a VIP nor instructed buys no model turn; ` +
      `by default it is only listed, and it waits for the digest or rings only as their importance and the modes they may interrupt in say.</div></div>` +
      this._instructionField(f, busy) +
      `<div class="actions"><button class="btn primary" data-act="save"${busy}>${isNew ? 'Add the contact' : 'Save'}</button>` +
      `<button class="btn" data-act="cancel"${busy}>Cancel</button></div>` +
      (this._error ? `<div class="err">${esc(this._error)}</div>` : '') +
      `</div>`;
  }

  // The instruction: the user's standing brief for this person's messages.
  // A model works each one by it — drafts, calendar entries, flags — and its
  // verdict sets how important the message is, which is what decides whether
  // it rings (unless they are a VIP, who rings at once regardless).
  _instructionField(f, busy) {
    const others = ((this._data && this._data.contacts) || [])
      .filter((c) => c.instruction && c.id !== this._open);
    const copy = others.length
      ? `<select data-set="instruction-from"${busy}><option value="">Copy instruction from…</option>` +
        others.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('') + `</select>`
      : '';
    return `<div><div class="k">Instruction</div>` +
      `<textarea data-set="instruction" rows="3" maxlength="2000"${busy} ` +
      `placeholder="e.g. Draft a reply to every message. When a visit moves, add it to the calendar — you have my approval for that. Ring me only if it is about today or tomorrow.">` +
      `${esc(f.instruction || '')}</textarea>` +
      (copy ? `<div class="chips" style="margin-top:6px">${copy}</div>` : '') +
      `<div class="note">A model works each of their messages by this, on every channel above — a mail from one of ` +
      `their addresses or domains included: it stages a ` +
      `reply in the chat's draft (it sends a mail only where this says to, under the mailbox's send policy, and never a ` +
      `message), takes the actions you name, and sets how important the message is — ` +
      `which decides whether it rings, after the work is done. If that work fails or takes longer than a minute ` +
      `(ten for a mail), the message rings anyway. What they write decides how loudly it rings, so use this for senders you trust — and think twice ` +
      `on SMS and e-mail, where a sender is easily faked.</div></div>`;
  }

  render() {
    const root = this.shadowRoot;
    if (!root) return;
    const d = this._data;
    const list = (d && d.contacts) || [];
    const active = root.activeElement && root.activeElement.getAttribute('data-set') === 'q';
    let body;
    if (!d) body = `<p class="muted">${this._error ? '' : 'Loading…'}</p>`;
    else {
      const rows = list.map((c) => (this._open === c.id ? `<li>${this._editor()}</li>` : this._row(c))).join('');
      body = (this._open === 'new' ? `<ul style="margin-bottom:10px"><li>${this._editor()}</li></ul>` : '') +
        (rows ? `<ul>${rows}</ul>` : `<p class="muted">${this._q ? 'Nobody matches.' : 'Nobody in the address book yet.'}</p>`);
    }
    root.innerHTML = `<style>${CSS}</style>` +
      `<header><h2>Contacts${list.length ? ` · ${list.length}` : ''}</h2>` +
      `<button class="btn tiny" data-act="new"${this._busy ? ' disabled' : ''}>+ New contact</button></header>` +
      `<input type="search" data-set="q" value="${esc(this._q)}" placeholder="Search names, e-mail addresses and domains" autocomplete="off">` +
      (this._error && !this._form ? `<div class="err">${esc(this._error)}</div>` : '') + body +
      this._pickHtml();
    if (active) {
      const q = root.querySelector('[data-set="q"]');
      q.focus();
      q.setSelectionRange(q.value.length, q.value.length);
    }
    if (this._pick && !this._pick.busy) {
      const first = root.querySelector('.pick .opt, .pick .btn');
      if (first && typeof first.focus === 'function') first.focus();
    }
  }
}

customElements.define('retinue-contacts', RetinueContacts);
