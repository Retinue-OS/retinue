// The address book: every person Retinue knows, whatever channel reaches them
// (docs/contacts.md). A contact is one file in one chamber; this page lists
// them, finds them by name or address, and edits what a person carries:
//
//   - their handles — e-mail addresses, phone numbers, Signal / WhatsApp /
//     Telegram accounts (or any other channel by name);
//   - the attention model's facts about them — the sphere they belong to and
//     further ones, their importance prior, the Focus modes they may
//     interrupt;
//   - the VIP flag: a model works their messages the moment they arrive, on
//     every channel (their e-mail addresses are whitelisted).
//
// Live endpoints, never cached by the service worker:
//   GET  /contacts[?q=]         -> { locations, default, contacts }
//   POST /contacts              -> create (needs `chamber`)
//   POST /contacts/<id>         -> change
//   GET  /attention/profile     -> { focus: { spheres, modes } } for the choices
//
// A new contact always names the chamber it is kept in; the first location in
// chambers.json is pre-selected.

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
  input[type=search], input[type=text], input[type=email], select {
    font: inherit; color: var(--fg, #e7ebf2); background: var(--card-2, #1c2230);
    border: 1px solid var(--line, rgba(231, 235, 242, .12)); border-radius: 10px; padding: 8px 10px; min-width: 0; }
  input[type=search] { width: 100%; margin-bottom: 10px; }
  .btn { font: inherit; font-size: .84rem; color: var(--fg, #e7ebf2); background: var(--card-2, #1c2230);
         border: 1px solid var(--line, rgba(231, 235, 242, .12)); border-radius: 999px; padding: 6px 12px; cursor: pointer; }
  .btn.primary { background: var(--accent, #6ea8fe); color: #0b0d12; border-color: transparent; font-weight: 600; }
  .btn.tiny { font-size: .76rem; padding: 3px 9px; }
  .btn.on { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }
  .btn[disabled] { opacity: .5; cursor: default; }
  ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px;
       grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); align-content: start; }
  li > button.row { all: unset; display: block; width: 100%; box-sizing: border-box; cursor: pointer;
       padding: 10px 12px; border-radius: 12px; background: var(--card-2, #1c2230);
       -webkit-tap-highlight-color: transparent; }
  @media (hover: hover) { li > button.row:hover { outline: 1px solid var(--accent, #6ea8fe); } }
  .name { color: var(--fg, #e7ebf2); font-weight: 600; }
  .vip { display: inline-block; font-size: .66rem; font-weight: 700; letter-spacing: .06em;
         color: #0b0d12; background: #f5c542; border-radius: 6px; padding: 1px 5px; margin-left: 6px; vertical-align: 2px; }
  .meta { font-size: .76rem; color: var(--muted, #8b93a3); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .editor { background: var(--card, #151922); border: 1px solid var(--accent, #6ea8fe); border-radius: 14px;
            padding: 12px; display: grid; gap: 12px; }
  .editor .k { font-size: .7rem; font-weight: 600; letter-spacing: .05em; text-transform: uppercase;
               color: var(--muted, #8b93a3); margin-bottom: 5px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .handle { display: flex; align-items: center; gap: 6px; font-size: .86rem; }
  .handle .ch { color: var(--muted, #8b93a3); min-width: 72px; }
  .handle .x { all: unset; cursor: pointer; color: var(--muted, #8b93a3); padding: 0 6px; }
  .add { display: flex; gap: 6px; flex-wrap: wrap; }
  .add input { flex: 1 1 160px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  input[data-set="name"] { width: 100%; font-weight: 600; }
`;

function sphereList(p) {
  return [p.sphere, ...(p.tags || [])].filter(Boolean);
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
  }

  connectedCallback() {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.addEventListener('click', (e) => this._onClick(e));
      this.shadowRoot.addEventListener('input', (e) => this._onInput(e));
      this.shadowRoot.addEventListener('change', (e) => this._onInput(e));
    }
    this.render();
    this.load();
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
          permits: [...(c.permits || [])], vip: Boolean(c.vip), handles: c.handles.map((h) => ({ ...h })),
          chamber: c.chamber, add: { channel: 'email', handle: '' } }
      : { name: '', sphere: '', tags: [], importance: null, permits: [], vip: false, handles: [],
          chamber: (this._data && this._data.default) || '', add: { channel: 'email', handle: '' } };
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
    else if (what === 'importance' && e.type === 'change') {
      this._form.importance = el.value === '' ? null : Number(el.value);
    }
  }

  _onClick(e) {
    const el = e.target.closest('[data-act]');
    if (!el || this._busy) return;
    const act = el.getAttribute('data-act');
    const f = this._form;
    const arg = el.getAttribute('data-v');
    switch (act) {
      case 'open': this._startEdit(el.getAttribute('data-id')); break;
      case 'new': this._startEdit('new'); break;
      case 'cancel': this._open = null; this._form = null; this._error = ''; this.render(); break;
      case 'chamber': if (f) { f.chamber = arg; this.render(); } break;
      case 'sphere': if (f) { f.sphere = f.sphere === arg ? '' : arg; f.tags = f.tags.filter((t) => t !== f.sphere); this.render(); } break;
      case 'tag': if (f) { f.tags = f.tags.includes(arg) ? f.tags.filter((t) => t !== arg) : f.tags.concat([arg]); this.render(); } break;
      case 'permit': if (f) { f.permits = f.permits.includes(arg) ? f.permits.filter((m) => m !== arg) : f.permits.concat([arg]); this.render(); } break;
      case 'vip': if (f) { f.vip = !f.vip; this.render(); } break;
      case 'drop-handle': if (f) { f.handles.splice(Number(arg), 1); this.render(); } break;
      case 'add-handle': this._addHandle(); break;
      case 'save': this._save(); break;
      default: break;
    }
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

  async _save() {
    const f = this._form;
    if (!f) return;
    if ((f.add.handle || '').trim()) this._addHandle();
    if (!f.name.trim()) { this._error = 'A name, so the person can be called something.'; this.render(); return; }
    const isNew = this._open === 'new';
    if (isNew && !f.chamber) { this._error = 'A chamber to keep the contact in.'; this.render(); return; }
    const facts = { name: f.name.trim(), sphere: f.sphere || null, tags: f.tags, importance: f.importance,
      permits: f.permits, vip: f.vip };
    let body;
    if (isNew) {
      body = { ...facts, chamber: f.chamber, handles: f.handles };
    } else {
      const was = this._contact(this._open) || { handles: [] };
      const key = (h) => `${h.channel}:${h.handle}`;
      const now = new Set(f.handles.map(key));
      const before = new Set(was.handles.map(key));
      body = { ...facts,
        add: f.handles.filter((h) => !before.has(key(h))),
        remove: was.handles.filter((h) => !now.has(key(h))) };
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
    const handles = (c.handles || []).map((h) => h.handle).join(' · ') || 'no handles';
    const spheres = sphereList(c);
    return `<li><button class="row" data-act="open" data-id="${esc(c.id)}">` +
      `<div class="name">${esc(c.name)}${c.vip ? '<span class="vip">VIP</span>' : ''}</div>` +
      `<div class="meta">${esc(handles)}</div>` +
      `<div class="meta">${spheres.length ? esc(spheres.join(', ')) + ' · ' : ''}${esc(c.chamber)}</div>` +
      `</button></li>`;
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
      ? `<div><div class="k">Kept in</div><div class="chips">${chambers.map((c) => chip('chamber', c, f.chamber === c)).join('') ||
        '<span class="note">No chamber holds contacts — see the contacts entry in chambers.json.</span>'}</div></div>`
      : `<div class="note">Kept in ${esc(f.chamber)}.</div>`;
    const handles = f.handles.map((h, i) =>
      `<div class="handle"><span class="ch">${esc(CHANNEL_LABEL[h.channel] || h.channel)}</span>` +
      `<span>${esc(h.handle)}</span><button class="x" data-act="drop-handle" data-v="${i}" aria-label="Remove">✕</button></div>`).join('');
    return `<div class="editor">` +
      `<input type="text" data-set="name" value="${esc(f.name)}" placeholder="Their name" autocomplete="off">` +
      where +
      `<div><div class="k">Reached by</div>${handles || '<div class="note">No handle yet.</div>'}` +
      `<div class="add" style="margin-top:6px"><select data-set="add-channel">` +
      CHANNELS.map((c) => `<option value="${c}"${f.add.channel === c ? ' selected' : ''}>${CHANNEL_LABEL[c]}</option>`).join('') +
      `</select><input type="text" data-set="add-handle" value="${esc(f.add.handle)}" placeholder="address, +41 …, @name" autocomplete="off">` +
      `<button class="btn tiny" data-act="add-handle"${busy}>Add</button></div></div>` +
      `<div><div class="k">Belongs to</div><div class="chips">${spheres.map((s) => chip('sphere', s, f.sphere === s)).join('')}</div>` +
      `<div class="note">The sphere decides which modes let them through.</div></div>` +
      `<div><div class="k">Also</div><div class="chips">${spheres.filter((s) => s !== f.sphere).map((s) => chip('tag', s, f.tags.includes(s))).join('')}</div></div>` +
      `<div><div class="k">Importance</div><select data-set="importance"${busy}>` +
      IMPORTANCE.map(([v, l]) => `<option value="${v == null ? '' : v}"${(f.importance == null ? v == null : Number(f.importance) === v) ? ' selected' : ''}>${l}</option>`).join('') +
      `</select> <span class="note">the prior for a message of theirs; the sheet's − / + teach it too</span></div>` +
      `<div><div class="k">May interrupt in</div><div class="chips">${modes.map(([id, m]) => chip('permit', id, f.permits.includes(id), (m && m.name) || id)).join('')}</div></div>` +
      `<div><div class="chips">${chip('vip', 'vip', f.vip, 'VIP')}</div>` +
      `<div class="note">A model works a VIP's messages the moment they arrive — on every channel above; their e-mail addresses are handled on the frequent run.</div></div>` +
      `<div class="actions"><button class="btn primary" data-act="save"${busy}>${isNew ? 'Add the contact' : 'Save'}</button>` +
      `<button class="btn" data-act="cancel"${busy}>Cancel</button></div>` +
      (this._error ? `<div class="err">${esc(this._error)}</div>` : '') +
      `</div>`;
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
      `<input type="search" data-set="q" value="${esc(this._q)}" placeholder="Search names and e-mail addresses" autocomplete="off">` +
      (this._error && !this._form ? `<div class="err">${esc(this._error)}</div>` : '') + body;
    if (active) {
      const q = root.querySelector('[data-set="q"]');
      q.focus();
      q.setSelectionRange(q.value.length, q.value.length);
    }
  }
}

customElements.define('retinue-contacts', RetinueContacts);
