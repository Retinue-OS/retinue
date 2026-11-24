// <retinue-compose-sheet>: a new conversation started from the Chats page's
// ＋ (docs/mail-chats.md, "Writing" — *New mail*). The page asks the gateway
// what it can start (GET /chats/compose: the channels whose gateway takes a
// new conversation — today e-mail, with the mailboxes it can write from) and
// opens this sheet for the one picked:
//
//   From      the mailbox, a choice only where there are several
//   To, Cc    recipients as removable chips — typed (a comma, a semicolon,
//             Enter or leaving the field makes one), pasted ("Ann <a@x.org>,
//             b@y.org" makes two), or picked from the address book's
//             suggestions (GET /contacts?q=, the people whose e-mail
//             addresses match); an address is checked by its shape only —
//             the mailbox is what knows whether it exists
//   Subject   one line
//   Text      the mail, in lines: Enter breaks one; the button or Ctrl/⌘+Enter
//             sends
//
// Sending is POST /chats/compose `{channel, account, to, cc, subject, text}`.
// The gateway mints the mail's Message-ID, which becomes the new chat's key,
// and answers `{id, message}` (sent) or `{id, pending}` (queued under the
// account's send policy); either way the page goes to the new chat — which
// may hold only the pending bubble of the mail waiting for approval. A
// refusal is shown in the sheet, in place, with everything typed still there.
//
// The sheet is a modal dialog: the page behind it is inert while it is open,
// focus starts in To and stays inside (Tab wraps), Escape closes it (a first
// Escape only shuts an open suggestion list), and focus goes back to what
// opened it. Closing is not discarding: what the sheet holds is kept on the
// device (drafts.js, `compose:<channel>`) and is there when it opens again;
// Discard empties it, a send that went through drops it.
//
// Everything a field holds is the user's own input, an address book entry or
// the server's error line: all of it is escaped as text, and none of it ever
// becomes markup, a link or a URL of this page's — the one URL built here is
// the new chat's page, from the id the server answered, percent-encoded as a
// query value.

import { esc } from './base.js';
import { loadDraft, saveDraft } from './drafts.js';

const COMPOSE_URL = '/chats/compose';
const CONTACTS_URL = '/contacts';
// Suggestions are asked for once typing pauses, from this many characters.
const SUGGEST_MS = 200;
const SUGGEST_MIN = 2;
const SUGGEST_MAX = 8;
// More recipients than a person writes to; the gateway has its own limits.
const MAX_RECIPIENTS = 50;
// RFC 5321's limit on a path.
const ADDRESS_MAX = 254;
const FIELDS = ['to', 'cc'];

// An e-mail address by its shape: something@domain.tld, no spaces, none of
// the characters that delimit addresses in a header, a domain of dotted
// labels whose last is two characters or more. Letters of any script are
// letters (an internationalised address is an address); whether the mailbox
// exists is the server's and the recipient's business.
const ADDRESS_RE = /^[^\s@<>()[\]\\,;:"]+@(?:[^\s@<>()[\]\\,;:".]+\.)+[^\s@<>()[\]\\,;:".]{2,}$/u;

export function isAddress(s) {
  return typeof s === 'string' && s.length <= ADDRESS_MAX && ADDRESS_RE.test(s);
}

// What the user typed or pasted into a recipient field, split into the
// addresses it names: on commas, semicolons and line breaks outside quotes
// and angle brackets, so `"Doe, Jane" <jane@x.org>, b@y.org` is two. A token
// with an address in angle brackets is that address, its display name the
// rest; any other token is itself. → {tokens: [{raw, address, name}], tail}:
// `tail` is whether the last token was not closed by a separator — the one
// the user is still typing.
export function splitAddresses(text) {
  const tokens = [];
  const push = (tok) => {
    const t = tok.trim();
    if (!t) return;
    const m = /^(.*)<([^<>]*)>\s*$/su.exec(t);
    if (m) {
      const name = m[1].trim().replace(/^"(.*)"$/su, '$1').trim();
      tokens.push({ raw: t, address: m[2].trim(), name });
    } else {
      tokens.push({ raw: t, address: t, name: '' });
    }
  };
  let cur = '';
  let quoted = false;
  let angle = false;
  for (const ch of String(text || '')) {
    if (ch === '"' && !angle) quoted = !quoted;
    else if (ch === '<' && !quoted) angle = true;
    else if (ch === '>' && !quoted) angle = false;
    if (!quoted && !angle && (ch === ',' || ch === ';' || ch === '\n' || ch === '\r')) {
      push(cur);
      cur = '';
      continue;
    }
    cur += ch;
  }
  const tail = !!cur.trim();
  push(cur);
  return { tokens, tail };
}

function invalidText(list) {
  return list.length === 1
    ? `“${list[0]}” is not an e-mail address.`
    : `These are not e-mail addresses: ${list.map((x) => `“${x}”`).join(', ')}.`;
}

// Open the sheet for one offer of GET /chats/compose (`{channel, label,
// accounts}`); `restoreFocus` is called when it closes, to put the focus
// back where the user was (the ＋, which a list refresh may have rebuilt).
export function openComposeSheet(offer, restoreFocus = null) {
  let el = document.querySelector('retinue-compose-sheet');
  if (!el) {
    el = document.createElement('retinue-compose-sheet');
    document.body.appendChild(el);
  }
  el.open(offer, restoreFocus);
  return el;
}

class RetinueComposeSheet extends HTMLElement {
  constructor() {
    super();
    this._offer = null;        // {channel, label, accounts}
    this._restoreFocus = null;
    this._chips = { to: [], cc: [] };   // {address, name}
    this._typed = { to: '', cc: '' };   // what a recipient field holds, not yet a chip
    this._subject = '';
    this._text = '';
    this._account = '';
    this._errors = {};         // field (to | cc | text | form) → its line
    this._busy = false;
    this._restored = false;    // the sheet opened on what an earlier one kept
    // The open suggestion list: which field it belongs to, its entries and
    // the one the arrow keys are on (-1: none).
    this._sugg = { field: '', items: [], active: -1 };
    this._suggSeq = 0;
    this._suggTimer = null;
    this._inerted = [];
    this._wired = false;
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    if (!this.hasAttribute('hidden') && !this._offer) this.setAttribute('hidden', '');
    this._wire();
  }

  disconnectedCallback() {
    this._uninert();
    if (this._suggTimer) clearTimeout(this._suggTimer);
    this._suggTimer = null;
  }

  get _key() { return `compose:${(this._offer && this._offer.channel) || ''}`; }

  open(offer, restoreFocus = null) {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._wire();
    const o = offer && typeof offer === 'object' ? offer : {};
    const accounts = Array.isArray(o.accounts) ? o.accounts.filter((a) => typeof a === 'string' && a) : [];
    this._offer = {
      channel: typeof o.channel === 'string' ? o.channel : 'email',
      label: typeof o.label === 'string' && o.label.trim() ? o.label.trim() : 'E-mail',
      accounts,
    };
    this._restoreFocus = restoreFocus;
    this._chips = { to: [], cc: [] };
    this._typed = { to: '', cc: '' };
    this._subject = '';
    this._text = '';
    this._errors = {};
    this._busy = false;
    this._closeSugg(false);
    // What an earlier sheet kept on this device comes back.
    const kept = loadDraft(this._key);
    this._restored = false;
    if (kept) {
      for (const f of FIELDS) {
        this._chips[f] = (Array.isArray(kept[f]) ? kept[f] : [])
          .filter((r) => r && isAddress(r.address))
          .map((r) => ({ address: r.address, name: typeof r.name === 'string' ? r.name : '' }));
        this._typed[f] = kept.typed && typeof kept.typed[f] === 'string' ? kept.typed[f] : '';
      }
      this._subject = typeof kept.subject === 'string' ? kept.subject : '';
      this._text = typeof kept.text === 'string' ? kept.text : '';
      this._account = typeof kept.account === 'string' ? kept.account : '';
      this._restored = !this._empty();
    }
    if (!accounts.includes(this._account)) this._account = accounts[0] || '';
    this.removeAttribute('hidden');
    this._render();
    this._inert();
    this._focus('[data-input="to"]');
  }

  // Shut the sheet, keeping what it holds for next time.
  close() {
    if (this.hasAttribute('hidden')) return;
    this._saveLocal();
    this._closeSugg(false);
    this.setAttribute('hidden', '');
    this._uninert();
    const back = this._restoreFocus;
    this._restoreFocus = null;
    if (typeof back === 'function') {
      try { back(); } catch (_e) { /* the page moved on */ }
    }
  }

  _empty() {
    return !this._chips.to.length && !this._chips.cc.length && !this._typed.to.trim()
      && !this._typed.cc.trim() && !this._subject.trim() && !this._text.trim();
  }

  _saveLocal() {
    if (!this._offer) return;
    saveDraft(this._key, this._empty() ? null : {
      to: this._chips.to, cc: this._chips.cc, typed: { ...this._typed },
      subject: this._subject, text: this._text, account: this._account,
    });
  }

  // The page behind a modal is out of reach: every other child of the body
  // is inert while the sheet is open (the sheet is the body's own child).
  _inert() {
    this._uninert();
    const body = document.body;
    const kids = body && body.children ? [...body.children] : [];
    for (const k of kids) {
      if (k === this || k.inert) continue;
      k.inert = true;
      this._inerted.push(k);
    }
  }

  _uninert() {
    for (const k of this._inerted) k.inert = false;
    this._inerted = [];
  }

  _focus(sel) {
    const el = this.shadowRoot && this.shadowRoot.querySelector(sel);
    if (el && el.focus) el.focus();
  }

  // ── Rendering ──────────────────────────────────────────────────────────────
  _render() {
    const o = this._offer;
    const accounts = o.accounts;
    let from = '';
    if (accounts.length > 1) {
      from = `<div class="row-f"><label class="lbl" for="compose-from">From</label>` +
        `<select id="compose-from" data-account>${accounts.map((a) =>
          `<option value="${esc(a)}"${a === this._account ? ' selected' : ''}>${esc(a)}</option>`).join('')}` +
        `</select></div>`;
    } else if (accounts.length === 1) {
      from = `<p class="from">From <b>${esc(accounts[0])}</b></p>`;
    }
    const restored = this._restored
      ? '<p class="note" role="status">The mail you started earlier is back.</p>' : '';
    this.shadowRoot.innerHTML = `<style>${CSS}</style>` +
      `<div class="overlay" data-overlay>` +
      `<form class="sheet" role="dialog" aria-modal="true" aria-labelledby="compose-title" data-form novalidate>` +
      `<div class="head"><h2 id="compose-title">New ${esc(/^e-mail$/i.test(o.label) ? 'e-mail' : o.label)}</h2>` +
      `<button type="button" class="x" data-close title="Close — what you wrote stays here for later" ` +
      `aria-label="Close — what you wrote stays here for later">&#10005;</button></div>` +
      restored + from +
      this._recipHtml('to', 'To') + this._recipHtml('cc', 'Cc') +
      `<div class="row-f"><label class="lbl" for="compose-subject">Subject</label>` +
      `<input id="compose-subject" data-subject type="text" autocomplete="off" value="${esc(this._subject)}"></div>` +
      `<div class="row-f"><label class="lbl" for="compose-text">Message</label>` +
      `<textarea id="compose-text" data-text rows="8" enterkeyhint="enter" ` +
      `aria-describedby="err-text compose-hint">${esc(this._text)}</textarea>` +
      `<p class="err" id="err-text" data-err="text" hidden></p></div>` +
      `<p class="hint" id="compose-hint">Enter starts a new line; Ctrl+Enter or &#8984;+Enter sends. ` +
      `Under the mailbox&rsquo;s send policy the mail may wait for your approval before it goes out.</p>` +
      `<p class="err form-err" data-err="form" role="alert" hidden></p>` +
      `<div class="actions">` +
      `<button type="button" class="ghost" data-discard title="Empty the form">Discard</button>` +
      `<button type="submit" class="send" data-send>Send</button>` +
      `</div></form></div>`;
    for (const f of FIELDS) this._paintChips(f);
    this._paintErrors();
    this._paintBusy();
  }

  _recipHtml(f, label) {
    return `<div class="row-f recip" data-recip="${f}">` +
      `<label class="lbl" for="compose-${f}">${label}</label>` +
      `<div class="chips-in">` +
      `<ul class="chips" data-chips="${f}" aria-label="${label}: the recipients so far"></ul>` +
      `<input id="compose-${f}" data-input="${f}" type="text" inputmode="email" autocomplete="off" ` +
      `autocapitalize="off" spellcheck="false" role="combobox" aria-autocomplete="list" ` +
      `aria-expanded="false" aria-controls="sugg-${f}" aria-describedby="err-${f}" ` +
      `placeholder="${f === 'to' ? 'Name or address' : 'Copy to …'}" value="${esc(this._typed[f])}">` +
      `</div>` +
      `<ul class="sugg" id="sugg-${f}" role="listbox" aria-label="Suggestions from the address book" ` +
      `data-sugg="${f}" hidden></ul>` +
      `<p class="err" id="err-${f}" data-err="${f}" hidden></p></div>`;
  }

  _chipsHtml(f) {
    return this._chips[f].map((r, i) => {
      const full = r.name ? `${r.name} <${r.address}>` : r.address;
      return `<li class="chip" title="${esc(r.address)}"><span class="chip-txt">${esc(r.name || r.address)}</span>` +
        `<button type="button" class="chip-x" data-remove="${f}" data-i="${i}" aria-label="Remove ${esc(full)}">` +
        `&#10005;</button></li>`;
    }).join('');
  }

  _paintChips(f) {
    const el = this.shadowRoot && this.shadowRoot.querySelector(`[data-chips="${f}"]`);
    if (el) {
      el.innerHTML = this._chipsHtml(f);
      el.hidden = !this._chips[f].length;
    }
  }

  // Every error line where it belongs, its field marked invalid with it.
  _paintErrors() {
    const root = this.shadowRoot;
    if (!root) return;
    for (const k of ['to', 'cc', 'text', 'form']) {
      const msg = this._errors[k] || '';
      const line = root.querySelector(`[data-err="${k}"]`);
      if (line) {
        line.textContent = msg;
        line.hidden = !msg;
      }
      const field = k === 'text' ? root.querySelector('[data-text]')
        : (k === 'form' ? null : root.querySelector(`[data-input="${k}"]`));
      if (field) {
        if (msg) field.setAttribute('aria-invalid', 'true');
        else field.removeAttribute('aria-invalid');
      }
    }
  }

  _paintBusy() {
    const btn = this.shadowRoot && this.shadowRoot.querySelector('[data-send]');
    if (!btn) return;
    btn.disabled = this._busy;
    btn.textContent = this._busy ? 'Sending …' : 'Send';
  }

  _paintSugg() {
    const root = this.shadowRoot;
    if (!root) return;
    for (const f of FIELDS) {
      const list = root.querySelector(`[data-sugg="${f}"]`);
      const input = root.querySelector(`[data-input="${f}"]`);
      const mine = this._sugg.field === f && this._sugg.items.length > 0;
      if (list) {
        list.innerHTML = mine ? this._sugg.items.map((s, i) =>
          `<li role="option" id="sugg-${f}-${i}" class="opt${i === this._sugg.active ? ' on' : ''}" ` +
          `data-pick="${f}" data-i="${i}" aria-selected="${i === this._sugg.active}">` +
          (s.name ? `<span class="opt-name">${esc(s.name)}</span> ` : '') +
          `<span class="opt-addr">${esc(s.address)}</span></li>`).join('') : '';
        list.hidden = !mine;
      }
      if (input) {
        input.setAttribute('aria-expanded', String(mine));
        if (mine && this._sugg.active >= 0) input.setAttribute('aria-activedescendant', `sugg-${f}-${this._sugg.active}`);
        else input.removeAttribute('aria-activedescendant');
      }
    }
  }

  // ── Recipients ─────────────────────────────────────────────────────────────
  // Addresses into field `f`'s chips: valid ones not there yet (an address
  // in To is not copied to itself in Cc), up to the cap. Returns the tokens
  // that are no address, as typed.
  _addAddresses(f, tokens) {
    const bad = [];
    const has = (list, a) => list.some((r) => r.address.toLowerCase() === a.toLowerCase());
    for (const t of tokens) {
      if (!isAddress(t.address)) { bad.push(t.raw); continue; }
      if (has(this._chips[f], t.address) || (f === 'cc' && has(this._chips.to, t.address))) continue;
      if (this._chips[f].length >= MAX_RECIPIENTS) {
        this._errors[f] = `At most ${MAX_RECIPIENTS} recipients here.`;
        break;
      }
      this._chips[f].push({ address: t.address, name: t.name || '' });
    }
    return bad;
  }

  // What field `f` holds turned into chips. `all`: the whole of it (Enter,
  // leaving the field, a send); otherwise what a separator has closed, the
  // token still being typed left in the field. What is no address stays in
  // the field, with a line saying so.
  _commit(f, all = true) {
    const { tokens, tail } = splitAddresses(this._typed[f]);
    const keep = !all && tail ? tokens.pop() : null;
    if (!tokens.length && !all) return;
    const bad = this._addAddresses(f, tokens);
    if (bad.length) this._errors[f] = invalidText(bad);
    else if (this._errors[f] && !this._errors[f].startsWith('At most')) delete this._errors[f];
    this._typed[f] = [...bad, ...(keep ? [keep.raw] : [])].join(', ');
    const input = this.shadowRoot && this.shadowRoot.querySelector(`[data-input="${f}"]`);
    if (input && input.value !== this._typed[f]) input.value = this._typed[f];
    this._paintChips(f);
    this._paintErrors();
    this._saveLocal();
  }

  _removeChip(f, i) {
    if (!FIELDS.includes(f) || !(i >= 0 && i < this._chips[f].length)) return;
    this._chips[f].splice(i, 1);
    if (this._errors[f] && this._errors[f].startsWith('At most')) delete this._errors[f];
    this._paintChips(f);
    this._paintErrors();
    this._saveLocal();
    // The button pressed is gone: the field it belonged to takes the focus.
    this._focus(`[data-input="${f}"]`);
  }

  _pick(f, i) {
    const s = this._sugg.field === f ? this._sugg.items[i] : null;
    if (!s) return;
    this._addAddresses(f, [{ raw: s.address, address: s.address, name: s.name }]);
    this._typed[f] = '';
    const input = this.shadowRoot && this.shadowRoot.querySelector(`[data-input="${f}"]`);
    if (input) input.value = '';
    delete this._errors[f];
    this._closeSugg();
    this._paintChips(f);
    this._paintErrors();
    this._saveLocal();
    this._focus(`[data-input="${f}"]`);
  }

  _closeSugg(paint = true) {
    this._suggSeq += 1;
    if (this._suggTimer) clearTimeout(this._suggTimer);
    this._suggTimer = null;
    this._sugg = { field: '', items: [], active: -1 };
    if (paint) this._paintSugg();
  }

  // Ask the address book once typing in field `f` pauses on a fragment that
  // could be a name or the start of an address.
  _scheduleSuggest(f) {
    if (this._suggTimer) clearTimeout(this._suggTimer);
    this._suggTimer = null;
    const q = this._typed[f].trim();
    if (q.length < SUGGEST_MIN || /[,;<>"]/.test(q)) {
      if (this._sugg.field) this._closeSugg();
      return;
    }
    this._suggTimer = setTimeout(() => this._suggest(f, q), SUGGEST_MS);
  }

  // The people whose name or address matches `q`, by their e-mail addresses
  // — those not in the field yet. An answer that arrives after the field
  // moved on (another fragment, a pick, the sheet closed) is dropped.
  async _suggest(f, q) {
    this._suggSeq += 1;
    const seq = this._suggSeq;
    try {
      const res = await fetch(`${CONTACTS_URL}?q=${encodeURIComponent(q)}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const doc = await res.json();
      if (seq !== this._suggSeq || this._typed[f].trim() !== q || this.hasAttribute('hidden')) return;
      const taken = new Set(this._chips[f].map((r) => r.address.toLowerCase()));
      const items = [];
      for (const c of Array.isArray(doc && doc.contacts) ? doc.contacts : []) {
        const name = c && typeof c.name === 'string' ? c.name.trim() : '';
        for (const h of (c && Array.isArray(c.handles) ? c.handles : [])) {
          const a = h && h.channel === 'email' && typeof h.handle === 'string' ? h.handle.trim() : '';
          if (!isAddress(a) || taken.has(a.toLowerCase())) continue;
          taken.add(a.toLowerCase());
          items.push({ name, address: a });
        }
      }
      this._sugg = { field: f, items: items.slice(0, SUGGEST_MAX), active: -1 };
      this._paintSugg();
    } catch (_err) {
      // No suggestions is no error: the address can still be typed.
      if (seq === this._suggSeq) this._closeSugg();
    }
  }

  // ── Events ─────────────────────────────────────────────────────────────────
  // Delegated on the shadow root, once: a render replaces what is inside,
  // never the root.
  _wire() {
    if (this._wired || !this.shadowRoot) return;
    this._wired = true;
    const root = this.shadowRoot;
    root.addEventListener('input', (e) => this._onInput(e));
    root.addEventListener('change', (e) => {
      const t = e.target;
      if (t && t.hasAttribute && t.hasAttribute('data-account')) {
        this._account = t.value;
        this._saveLocal();
      }
    });
    root.addEventListener('keydown', (e) => this._onKeydown(e));
    root.addEventListener('focusout', (e) => {
      const t = e.target;
      const f = t && t.getAttribute ? t.getAttribute('data-input') : null;
      if (!FIELDS.includes(f)) return;
      // Leaving a recipient field makes a chip of what it holds — unless the
      // focus went to its own suggestion list (a press on an option).
      this._commit(f, true);
      if (this._sugg.field === f) this._closeSugg();
    });
    // A press on an option or a chip's ✕ must not take the focus from the
    // field first (its focusout would commit the fragment being picked).
    root.addEventListener('mousedown', (e) => {
      const t = e.target && e.target.closest ? e.target.closest('[data-pick]') : null;
      if (t) e.preventDefault();
    });
    root.addEventListener('click', (e) => this._onClick(e));
    root.addEventListener('submit', (e) => {
      e.preventDefault();
      this._submit();
    });
  }

  _onInput(e) {
    const t = e.target;
    if (!t || !t.getAttribute) return;
    const f = t.getAttribute('data-input');
    if (FIELDS.includes(f)) {
      this._typed[f] = t.value;
      // A separator typed or pasted closes the addresses before it.
      if (/[,;\n]/.test(t.value)) this._commit(f, false);
      this._scheduleSuggest(f);
      this._saveLocal();
      return;
    }
    if (t.hasAttribute('data-subject')) this._subject = t.value;
    else if (t.hasAttribute('data-text')) {
      this._text = t.value;
      if (this._errors.text && t.value.trim()) {
        delete this._errors.text;
        this._paintErrors();
      }
    } else return;
    this._saveLocal();
  }

  _onClick(e) {
    const at = (sel) => (e.target && e.target.closest ? e.target.closest(sel) : null);
    const rm = at('[data-remove]');
    if (rm) {
      this._removeChip(rm.getAttribute('data-remove'), Number(rm.getAttribute('data-i')));
      return;
    }
    const pick = at('[data-pick]');
    if (pick) {
      this._pick(pick.getAttribute('data-pick'), Number(pick.getAttribute('data-i')));
      return;
    }
    if (at('[data-close]')) {
      this.close();
      return;
    }
    if (at('[data-discard]')) {
      this._discard();
      return;
    }
    // A press on the dimmed page around the sheet shuts it, as Escape does.
    const t = e.target;
    if (t && t.hasAttribute && t.hasAttribute('data-overlay')) this.close();
  }

  _onKeydown(e) {
    const t = e.target;
    const f = t && t.getAttribute ? t.getAttribute('data-input') : null;
    if (FIELDS.includes(f)) this._recipKey(e, f);
    else if (t && t.hasAttribute && t.hasAttribute('data-subject') && e.key === 'Enter') {
      // A subject is one line: Enter goes on to the text, never sends.
      e.preventDefault();
      this._focus('[data-text]');
    } else if (t && t.hasAttribute && t.hasAttribute('data-text') && e.key === 'Enter'
      && (e.ctrlKey || e.metaKey) && !e.isComposing) {
      e.preventDefault();
      this._submit();
    }
    if (e.defaultPrevented) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      this.close();
    } else if (e.key === 'Tab') {
      this._trapTab(e);
    }
  }

  // The keys of a recipient field: the arrows walk an open suggestion list,
  // Enter picks the one they are on or makes chips of what is typed (never
  // submits the form), Escape shuts the list (and only the list), Backspace
  // in an empty field takes the last chip back.
  _recipKey(e, f) {
    const open = this._sugg.field === f && this._sugg.items.length > 0;
    const n = this._sugg.items.length;
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && open) {
      e.preventDefault();
      const step = e.key === 'ArrowDown' ? 1 : -1;
      const at = this._sugg.active;
      this._sugg.active = at < 0 ? (step > 0 ? 0 : n - 1) : (at + step + n) % n;
      this._paintSugg();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (e.isComposing) return;
      if (open && this._sugg.active >= 0) this._pick(f, this._sugg.active);
      else if (this._typed[f].trim()) {
        this._commit(f, true);
        this._closeSugg();
      }
    } else if (e.key === 'Escape' && open) {
      e.preventDefault();
      this._closeSugg();
    } else if (e.key === 'Backspace' && !this._typed[f] && this._chips[f].length) {
      e.preventDefault();
      this._chips[f].pop();
      this._paintChips(f);
      this._saveLocal();
    }
  }

  // Tab stays inside the dialog: past its last control to its first, and
  // back.
  _trapTab(e) {
    const root = this.shadowRoot;
    const all = [...root.querySelectorAll('input, select, textarea, button, a[href]')]
      .filter((el) => !el.disabled && !el.hidden);
    if (!all.length) return;
    const first = all[0];
    const last = all[all.length - 1];
    const active = root.activeElement;
    if (e.shiftKey && (active === first || !active)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !active)) {
      e.preventDefault();
      first.focus();
    }
  }

  _discard() {
    this._chips = { to: [], cc: [] };
    this._typed = { to: '', cc: '' };
    this._subject = '';
    this._text = '';
    this._errors = {};
    this._restored = false;
    this._closeSugg(false);
    saveDraft(this._key, null);
    this._render();
    this._focus('[data-input="to"]');
  }

  // ── Sending ────────────────────────────────────────────────────────────────
  async _submit() {
    if (this._busy || !this._offer) return;
    for (const f of FIELDS) if (this._typed[f].trim()) this._commit(f, true);
    this._closeSugg();
    delete this._errors.form;
    if (!this._chips.to.length && !this._errors.to) this._errors.to = 'Add at least one recipient.';
    if (!this._text.trim()) this._errors.text = 'Write the mail before you send it.';
    else delete this._errors.text;
    const first = ['to', 'cc', 'text'].find((k) => this._errors[k]);
    this._paintErrors();
    if (first) {
      this._focus(first === 'text' ? '[data-text]' : `[data-input="${first}"]`);
      return;
    }
    this._busy = true;
    this._paintBusy();
    const body = {
      channel: this._offer.channel,
      to: this._chips.to.map((r) => r.address),
      cc: this._chips.cc.map((r) => r.address),
      subject: this._subject,
      text: this._text,
    };
    if (this._account) body.account = this._account;
    let error = '';
    try {
      const res = await fetch(COMPOSE_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      let doc = null;
      try { doc = await res.json(); } catch (_e) { doc = null; }
      const id = doc && typeof doc.id === 'string' ? doc.id : '';
      if ((res.status === 200 || res.status === 202) && id && id.length <= 2048) {
        // Sent or queued: the new chat is where it now lives — showing the
        // mail, or its pending bubble with the approval link.
        saveDraft(this._key, null);
        this._done = true;
        this._uninert();
        location.assign(`/chat.html?id=${encodeURIComponent(id)}`);
        return;
      }
      const said = doc && typeof doc.error === 'string' ? doc.error.trim().slice(0, 300) : '';
      if (res.ok) error = 'The server did not say where the new chat is — look for it in the chat list.';
      else if (said) error = `Not sent: ${said}`;
      else if (res.status === 403) error = 'Not sent: the server refused the request.';
      else error = `Not sent (the server answered ${res.status}). Try again in a moment.`;
    } catch (_err) {
      error = "Couldn't reach the server — check the connection and try again.";
    }
    this._busy = false;
    this._errors.form = error;
    this._paintBusy();
    this._paintErrors();
  }
}

const CSS = `
  :host { position: fixed; inset: 0; z-index: 55; display: block; }
  :host([hidden]) { display: none; }
  * { box-sizing: border-box; }
  button, select, input, textarea { font: inherit; }
  .overlay { position: absolute; inset: 0; background: rgba(0, 0, 0, .55);
             display: flex; align-items: flex-end; justify-content: center; }
  .sheet { background: var(--card, #151922); color: var(--fg, #e7ebf2); margin: 0;
           border-top: 1px solid var(--line, rgba(231, 235, 242, .12));
           border-radius: 20px 20px 0 0; width: 100%; max-width: 640px; max-height: 94vh; max-height: 94dvh;
           overflow: auto; padding: 14px 14px calc(env(safe-area-inset-bottom, 0px) + 16px);
           display: flex; flex-direction: column; gap: 10px;
           font: 15px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  @media (min-width: 700px) {
    .overlay { align-items: center; }
    .sheet { border-radius: 20px; border: 1px solid var(--line, rgba(231, 235, 242, .12)); }
  }
  .head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  h2 { margin: 0; font-size: 16px; font-weight: 700; }
  .x { color: var(--muted, #8b93a3); background: none; border: 0; font-size: 18px; cursor: pointer;
       padding: 2px 6px; border-radius: 8px; }
  .x:focus-visible, .ghost:focus-visible, .send:focus-visible, .chip-x:focus-visible {
    outline: 2px solid var(--accent, #6ea8fe); outline-offset: 2px; }
  .note, .from, .hint { margin: 0; font-size: 12.5px; color: var(--muted, #8b93a3); }
  .from b { color: var(--fg, #e7ebf2); font-weight: 600; }
  .row-f { display: flex; flex-direction: column; gap: 4px; position: relative; }
  .lbl { font-size: 11px; letter-spacing: 1px; text-transform: uppercase; color: var(--muted, #8b93a3); }
  input[type="text"], select, textarea {
    width: 100%; background: var(--bg, #0b0d12); color: var(--fg, #e7ebf2);
    border: 1px solid var(--line, rgba(231, 235, 242, .14)); border-radius: 10px; padding: 8px 10px; }
  textarea { resize: vertical; min-height: 9em; }
  input:focus-visible, select:focus-visible, textarea:focus-visible {
    outline: 2px solid var(--outbound, #f0b45e); outline-offset: 0; }
  [aria-invalid="true"] { border-color: #e0716b; }
  .chips-in { display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
              background: var(--bg, #0b0d12); border: 1px solid var(--line, rgba(231, 235, 242, .14));
              border-radius: 10px; padding: 4px; }
  .chips-in:focus-within { outline: 2px solid var(--outbound, #f0b45e); }
  .chips-in input[type="text"] { flex: 1 1 10em; min-width: 8em; border: 0; padding: 4px 6px;
                                 background: transparent; }
  .chips-in input[type="text"]:focus-visible { outline: none; }
  .chips { display: contents; list-style: none; margin: 0; padding: 0; }
  .chips[hidden] { display: none; }
  .chip { display: inline-flex; align-items: center; gap: 2px; max-width: 100%;
          background: var(--card-2, #1c2230); border: 1px solid var(--line, rgba(231, 235, 242, .14));
          border-radius: 999px; padding: 1px 2px 1px 10px; font-size: 13px; }
  .chip-txt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chip-x { background: none; border: 0; color: var(--muted, #8b93a3); cursor: pointer;
            padding: 2px 6px; border-radius: 999px; font-size: 12px; }
  .chip-x:hover { color: var(--fg, #e7ebf2); }
  .sugg { list-style: none; margin: 0; padding: 4px; position: absolute; left: 0; right: 0; top: 100%;
          z-index: 2; background: var(--card-2, #1c2230); border: 1px solid var(--line, rgba(231, 235, 242, .14));
          border-radius: 10px; max-height: 40vh; overflow: auto; box-shadow: 0 8px 24px rgba(0, 0, 0, .4); }
  .sugg[hidden] { display: none; }
  .opt { padding: 6px 8px; border-radius: 8px; cursor: pointer; overflow-wrap: anywhere; }
  .opt:hover, .opt.on { background: rgba(110, 168, 254, .16); }
  .opt-addr { color: var(--muted, #8b93a3); font-size: 13px; }
  .err { margin: 0; color: #f0a39e; font-size: 13px; }
  .err[hidden] { display: none; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; }
  .ghost, .send { border-radius: 999px; padding: 7px 16px; cursor: pointer; font-weight: 600; }
  .ghost { background: transparent; color: var(--muted, #8b93a3);
           border: 1px solid var(--line, rgba(231, 235, 242, .16)); }
  .send { background: var(--outbound, #f0b45e); color: #0b0d12; border: 0; }
  .send[disabled] { opacity: .6; cursor: progress; }
`;

customElements.define('retinue-compose-sheet', RetinueComposeSheet);
