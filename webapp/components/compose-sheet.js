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
// Sending is POST /chats/compose `{channel, account, to, cc, subject, text,
// request_key}`. The gateway mints the mail's Message-ID, which becomes the
// new chat's key, and answers `{id, message}` (sent) or `{id, pending}`
// (queued under the account's send policy); either way the page goes to the
// new chat — which may hold only the pending bubble of the mail waiting for
// approval. A refusal (4xx) is shown in the sheet, in place, with everything
// typed still there.
//
// One press is one mail. A press mints a `request_key` and holds the Send
// button until it is answered — closing the sheet and opening it again
// brings back the same sheet, still sending, never a second Send for the
// same mail. The server's word that nothing went out — any 4xx, and any
// answer that says `"sent": false` (the mail server refused the mail, the
// gateway was never reached) — is "Not sent": the press is over, and the
// next one is a new attempt under a new key. Where the answer leaves open
// whether the mail went out (202 `{unconfirmed}`, a 5xx that does not say
// so, no answer at all) the sheet says exactly that, never "not sent", and
// the press stays with the mail (also on the device): sent again unchanged
// it goes out under the same key, which the gateway answers from the first
// time instead of sending twice.
//
// The sheet is a modal dialog: the page behind it is inert while it is open,
// focus starts in To and stays inside (Tab wraps), Escape closes it (a first
// Escape only shuts an open suggestion list), and focus goes back to what
// opened it. Closing is not discarding: what the sheet holds is kept on the
// device (drafts.js) and is there when it opens again; Discard empties it, a
// send that went through drops it. The kept mail belongs to the mailbox it
// was started from (`compose:<channel>:<mailbox>`): it comes back only into
// a sheet writing from that mailbox, and never moves to another one unless
// the user picks that one under From. A mail kept for another mailbox than
// the one the sheet writes from is not forgotten: the sheet remembers which
// mailboxes hold one, says so each time it opens, and opens on such a
// mailbox when the one last written from has nothing waiting.
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
const REQUEST_KEY_RE = /^[A-Za-z0-9_-]{16,64}$/;
// How many other mailboxes with a kept mail the sheet remembers.
const MAX_PARKED = 20;

// An opaque token for one press of Send (see chat-page.js, newRequestKey).
function newRequestKey() {
  try {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') return globalThis.crypto.randomUUID();
    if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === 'function') {
      const b = globalThis.crypto.getRandomValues(new Uint8Array(16));
      return [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
    }
  } catch (_e) { /* fall through */ }
  let out = '';
  while (out.length < 32) out += Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, '0');
  return out.slice(0, 32);
}

// A key is composing text when an input method has it — also the Enter that
// commits a composition in WebKit, whose keydown arrives after
// compositionend (isComposing false) and is told only by keyCode 229.
function composing(e) {
  return !!e.isComposing || e.keyCode === 229;
}

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

// The addresses the cap kept out, named: they stay in the field.
function capText(over) {
  return `At most ${MAX_RECIPIENTS} recipients here; not added: ${over.join(', ')}.`;
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
    this._busy = false;        // a send is on its way; survives close() and open()
    this._done = false;        // it went through: the page is on its way to the chat
    // The press whose outcome is not known — on its way, or answered without
    // a word on whether the mail went out: {key, sig}, `sig` the request it
    // made. The same request again goes out under the same key.
    this._press = null;
    this._restored = false;    // the sheet opened on what an earlier one kept
    // The other mailboxes that hold a kept mail (`_parked`, kept in the
    // index so the next opening knows of them), and of those the ones not
    // offered now (`_waiting`).
    this._parked = [];
    this._waiting = [];
    this._moved = '';          // the mailbox the form was just switched away from
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

  // Where things are kept on the device. One mail per mailbox, under
  // `compose:<channel>:<mailbox>`; and under `compose:<channel>` the index
  // `{account, waiting}`: which mailbox was written from last, so the sheet
  // opens on it again, and which others hold a kept mail, so none of them
  // drops out of sight once another mailbox is written from.
  get _indexKey() { return `compose:${(this._offer && this._offer.channel) || ''}`; }

  _saveIndex() {
    saveDraft(this._indexKey, { account: this._account, waiting: this._parked });
  }

  _slotKey(account) { return `${this._indexKey}:${String(account || '').toLowerCase()}`; }

  get _key() { return this._slotKey(this._account); }

  open(offer, restoreFocus = null) {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._wire();
    this._restoreFocus = restoreFocus;
    // A send is on its way: this is that sheet, still sending. Never a fresh
    // form with Send armed again for the same mail — and what it holds is
    // not replaced by what was kept when it was closed, which is the mail in
    // flight itself.
    if (this._busy && this._offer) {
      this._show();
      return;
    }
    const o = offer && typeof offer === 'object' ? offer : {};
    const accounts = Array.isArray(o.accounts) ? o.accounts.filter((a) => typeof a === 'string' && a) : [];
    this._offer = {
      channel: typeof o.channel === 'string' ? o.channel : 'email',
      label: typeof o.label === 'string' && o.label.trim() ? o.label.trim() : 'E-mail',
      accounts,
    };
    this._errors = {};
    this._done = false;
    this._closeSugg(false);
    // The mailboxes that hold a kept mail: the one written from last and
    // those the index remembers beside it — each asked for its slot, since
    // a mail may have been sent or discarded since.
    const index = loadDraft(this._indexKey);
    const last = index && typeof index.account === 'string' ? index.account : '';
    const known = [last].concat(index && Array.isArray(index.waiting) ? index.waiting : [])
      .filter((a) => typeof a === 'string' && a && a.length <= ADDRESS_MAX).slice(0, MAX_PARKED + 1);
    const holding = known.filter((a, i) => known.findIndex((b) => b.toLowerCase() === a.toLowerCase()) === i
      && loadDraft(this._slotKey(a)));
    // The mailbox written from last, where it can be written from now and
    // has something waiting; else one that can and has (a mail kept while
    // its mailbox could not send comes back when it can again); else the
    // one written from last, or the first on offer.
    const offered = (a) => accounts.includes(a);
    this._account = holding.find(offered) || (offered(last) ? last : (accounts[0] || ''));
    // What an earlier sheet kept for this mailbox comes back — into this
    // mailbox only. A mail kept for another one stays where it is: it is
    // never taken into this mailbox, where one press would send it from
    // the wrong address — and the sheet says that it is there.
    this._loadKept();
    this._parked = holding.filter((a) => a.toLowerCase() !== this._account.toLowerCase());
    this._waiting = this._parked.filter((a) => !offered(a));
    // The index is told what was found — a mailbox whose mail was sent or
    // discarded since holds none any more — but not that anything was
    // written: the mailbox written from last stays that one, also while it
    // is not offered, until the user writes from another (_saveLocal).
    if (index) {
      const from = last || this._account;
      saveDraft(this._indexKey, { account: from, waiting: holding.filter((a) => a.toLowerCase() !== from.toLowerCase()) });
    }
    this._show();
  }

  _show() {
    this.removeAttribute('hidden');
    this._render();
    this._inert();
    this._focus('[data-input="to"]');
  }

  // The mail kept for the mailbox the sheet now writes from, into the form
  // (or an empty form where there is none).
  _loadKept() {
    this._chips = { to: [], cc: [] };
    this._typed = { to: '', cc: '' };
    this._subject = '';
    this._text = '';
    this._press = null;
    this._restored = false;
    const kept = loadDraft(this._key);
    if (!kept) return;
    for (const f of FIELDS) {
      this._chips[f] = (Array.isArray(kept[f]) ? kept[f] : [])
        .filter((r) => r && isAddress(r.address))
        .map((r) => ({ address: r.address, name: typeof r.name === 'string' ? r.name : '' }));
      this._typed[f] = kept.typed && typeof kept.typed[f] === 'string' ? kept.typed[f] : '';
    }
    this._subject = typeof kept.subject === 'string' ? kept.subject : '';
    this._text = typeof kept.text === 'string' ? kept.text : '';
    this._restored = !this._empty();
    // A press that was never answered stays with its mail.
    const press = kept.press;
    if (this._restored && press && typeof press === 'object' && typeof press.key === 'string'
      && REQUEST_KEY_RE.test(press.key) && typeof press.sig === 'string') {
      this._press = { key: press.key, sig: press.sig };
    }
  }

  // From changed, by the user's own choice. The mail in the form goes with
  // it — unless the mailbox chosen has a kept mail of its own: then that one
  // comes back and this one stays kept under the mailbox it was written
  // from, so neither is lost and neither changes its sender unseen. An empty
  // form simply opens on what the chosen mailbox holds.
  _switchAccount(next) {
    if (!this._offer || this._busy || next === this._account || !this._offer.accounts.includes(next)) return;
    const from = this._account;
    const theirs = loadDraft(this._slotKey(next));
    const not = (a) => (b) => b.toLowerCase() !== a.toLowerCase();
    if (!this._empty() && !theirs) {
      saveDraft(this._slotKey(from), null);
      this._account = next;
      this._parked = this._parked.filter(not(next));
      this._saveLocal();
      return;
    }
    const mine = !this._empty();
    this._saveLocal();
    this._account = next;
    this._errors = {};
    this._closeSugg(false);
    this._loadKept();
    // The mailbox left holds a mail now, if the form held one; the one
    // chosen has given its mail to the form.
    this._parked = this._parked.filter(not(next)).filter(not(from)).concat(mine ? [from] : []).slice(-MAX_PARKED);
    this._waiting = this._parked.filter((a) => !this._offer.accounts.includes(a));
    this._saveIndex();
    this._moved = mine ? from : '';
    this._render();
    this._moved = '';
    this._focus('[data-account]');
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
    if (this._empty()) {
      saveDraft(this._key, null);
      return;
    }
    saveDraft(this._key, {
      to: this._chips.to, cc: this._chips.cc, typed: { ...this._typed },
      subject: this._subject, text: this._text, account: this._account, press: this._press,
    });
    this._saveIndex();
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
    // What the sheet opened on, said: a mail in flight, one kept earlier
    // (and whether a press of it was never answered), one waiting for a
    // mailbox that is not offered now.
    let restored = '';
    if (this._busy) {
      restored = '<p class="note" role="status">This mail is being sent.</p>';
    } else if (this._restored) {
      restored = '<p class="note" role="status">The mail you started earlier is back.' +
        (this._moved ? ` What you wrote from ${esc(this._moved)} is kept for that mailbox.` : '') +
        (this._press ? ' It was being sent when this sheet was last open, and it is not confirmed ' +
          'whether it went out: look in the chat list before you send it again.' : '') + '</p>';
    }
    // The mails kept for other mailboxes, each named: one whose mailbox is
    // not offered now waits for it; one whose mailbox is, is a choice under
    // From away.
    for (const a of this._parked) {
      restored += `<p class="note" role="status">A mail you started from ${esc(a)} is kept. ` +
        (this._waiting.includes(a) ? 'It comes back when that mailbox can send again.'
          : 'Choose that mailbox under From to go on with it.') + '</p>';
    }
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
    btn.disabled = this._busy || this._done;
    btn.textContent = this._busy ? 'Sending …' : (this._done ? 'Opening the chat …' : 'Send');
    // While the mail is on its way it is neither emptied nor moved to
    // another mailbox; closing stays possible.
    for (const sel of ['[data-account]', '[data-discard]']) {
      const el = this.shadowRoot.querySelector(sel);
      if (el) el.disabled = this._busy || this._done;
    }
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
  // in To is not copied to itself in Cc), up to the cap. Returns, as typed,
  // the tokens that are no address (`bad`) and the addresses the cap kept
  // out (`over`) — neither is dropped: the caller leaves both in the field.
  _addAddresses(f, tokens) {
    const bad = [];
    const over = [];
    const has = (list, a) => list.some((r) => r.address.toLowerCase() === a.toLowerCase());
    for (const t of tokens) {
      if (!isAddress(t.address)) { bad.push(t.raw); continue; }
      if (has(this._chips[f], t.address) || (f === 'cc' && has(this._chips.to, t.address))) continue;
      if (this._chips[f].length >= MAX_RECIPIENTS) { over.push(t.raw); continue; }
      this._chips[f].push({ address: t.address, name: t.name || '' });
    }
    return { bad, over };
  }

  // What field `f` holds turned into chips. `all`: the whole of it (Enter,
  // leaving the field, a send); otherwise what a separator has closed, the
  // token still being typed left in the field. What is no address, and what
  // the cap kept out, stays in the field, with a line naming it; a field
  // that is clean has no error left.
  _commit(f, all = true) {
    const { tokens, tail } = splitAddresses(this._typed[f]);
    const keep = !all && tail ? tokens.pop() : null;
    if (!tokens.length && !all) return;
    const { bad, over } = this._addAddresses(f, tokens);
    const lines = [bad.length ? invalidText(bad) : '', over.length ? capText(over) : ''].filter(Boolean);
    if (lines.length) this._errors[f] = lines.join(' ');
    else delete this._errors[f];
    this._typed[f] = [...bad, ...over, ...(keep ? [keep.raw] : [])].join(', ');
    const input = this.shadowRoot && this.shadowRoot.querySelector(`[data-input="${f}"]`);
    if (input && input.value !== this._typed[f]) input.value = this._typed[f];
    this._paintChips(f);
    this._paintErrors();
    this._saveLocal();
  }

  _removeChip(f, i) {
    if (!FIELDS.includes(f) || !(i >= 0 && i < this._chips[f].length)) return;
    this._chips[f].splice(i, 1);
    // There is room again: what the field still holds is taken in with the
    // next commit, and the cap's line goes now.
    if (this._errors[f] && this._errors[f].includes('At most')) delete this._errors[f];
    this._paintChips(f);
    this._paintErrors();
    this._saveLocal();
    // The button pressed is gone: the field it belonged to takes the focus.
    this._focus(`[data-input="${f}"]`);
  }

  _pick(f, i) {
    const s = this._sugg.field === f ? this._sugg.items[i] : null;
    if (!s) return;
    const { over } = this._addAddresses(f, [{ raw: s.address, address: s.address, name: s.name }]);
    this._typed[f] = '';
    const input = this.shadowRoot && this.shadowRoot.querySelector(`[data-input="${f}"]`);
    if (input) input.value = '';
    if (over.length) this._errors[f] = capText(over);
    else delete this._errors[f];
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

  // Which recipient field has the focus, or null.
  _focusedField() {
    const a = this.shadowRoot && this.shadowRoot.activeElement;
    const f = a && a.getAttribute ? a.getAttribute('data-input') : null;
    return FIELDS.includes(f) ? f : null;
  }

  // The people whose name or address matches `q`, by their e-mail addresses
  // — those not in the field yet. An answer that arrives after the field
  // moved on (another fragment, a pick, the sheet closed) or was left is
  // dropped: a list under a field nobody is in is one nothing closes.
  async _suggest(f, q) {
    this._suggSeq += 1;
    const seq = this._suggSeq;
    try {
      const res = await fetch(`${CONTACTS_URL}?q=${encodeURIComponent(q)}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const doc = await res.json();
      if (seq !== this._suggSeq || this._typed[f].trim() !== q || this.hasAttribute('hidden')) return;
      if (this._focusedField() !== f) return;
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
      if (t && t.hasAttribute && t.hasAttribute('data-account')) this._switchAccount(t.value);
    });
    root.addEventListener('keydown', (e) => this._onKeydown(e));
    root.addEventListener('focusout', (e) => {
      const t = e.target;
      const f = t && t.getAttribute ? t.getAttribute('data-input') : null;
      if (!FIELDS.includes(f)) return;
      // Leaving a recipient field makes a chip of what it holds — a press
      // on one of its suggestions does not leave it (see mousedown below) —
      // and ends its suggestions: the open list, and a question still on its
      // way to the address book, whose answer would otherwise open a list
      // under a field that is no longer in use.
      this._commit(f, true);
      this._closeSugg();
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
      // An emptied field has nothing left to be wrong about.
      else if (!t.value.trim() && this._errors[f]) {
        delete this._errors[f];
        this._paintErrors();
      }
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
      // A subject is one line: Enter goes on to the text, never sends — and
      // an input method's Enter only commits what it was composing.
      e.preventDefault();
      if (!composing(e)) this._focus('[data-text]');
    } else if (t && t.hasAttribute && t.hasAttribute('data-text') && e.key === 'Enter'
      && (e.ctrlKey || e.metaKey) && !composing(e)) {
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
      if (composing(e)) return;
      if (open && this._sugg.active >= 0) this._pick(f, this._sugg.active);
      else if (this._typed[f].trim()) {
        this._commit(f, true);
        this._closeSugg();
      }
    } else if (e.key === 'Escape' && open) {
      e.preventDefault();
      this._closeSugg();
    } else if (e.key === 'Backspace' && !this._typed[f] && this._chips[f].length) {
      // As the chip's own ✕ would: the cap's line goes with it.
      e.preventDefault();
      this._removeChip(f, this._chips[f].length - 1);
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
    // Not the mail on its way: its answer would have nothing left to be about.
    if (this._busy) return;
    this._chips = { to: [], cc: [] };
    this._typed = { to: '', cc: '' };
    this._subject = '';
    this._text = '';
    this._errors = {};
    this._restored = false;
    this._press = null;
    this._closeSugg(false);
    saveDraft(this._key, null);
    this._render();
    this._focus('[data-input="to"]');
  }

  // ── Sending ────────────────────────────────────────────────────────────────
  // One press, one request, one key. While a request is on its way nothing
  // starts a second one — not a second press, not closing and opening the
  // sheet. The answer:
  //  - 200 / 202 with the new chat's id: sent, queued, or handed over and
  //    not confirmed yet — the chat is where the mail now lives and says
  //    which (its message, its pending bubble); the kept mail is dropped;
  //  - 4xx, or any answer that says `"sent": false`: the server's word that
  //    nothing went out, shown with its sentence. The press is over — the
  //    next one is a new attempt, under a new key;
  //  - anything else (202 `{unconfirmed}` without a chat, a 5xx that does
  //    not say so, no answer): it is not known whether the mail went out,
  //    and the sheet says so. The press stays with the mail: unchanged, it
  //    goes out again under the same key.
  // An answer that is not a success comes to a sheet that was closed
  // meanwhile by opening it again: a mail that did not verifiably go out is
  // not left unsaid.
  async _submit() {
    if (this._busy || this._done || !this._offer) return;
    for (const f of FIELDS) this._commit(f, true);
    this._closeSugg();
    delete this._errors.form;
    if (!this._chips.to.length && !this._errors.to) this._errors.to = 'Add at least one recipient.';
    if (!this._text.trim()) this._errors.text = 'Write the mail before you send it.';
    else delete this._errors.text;
    const accounts = this._offer.accounts;
    if (accounts.length && !accounts.includes(this._account)) {
      this._errors.form = 'Choose the mailbox this mail leaves from.';
    }
    const first = ['to', 'cc', 'text'].find((k) => this._errors[k]);
    this._paintErrors();
    if (first || this._errors.form) {
      if (first) this._focus(first === 'text' ? '[data-text]' : `[data-input="${first}"]`);
      return;
    }
    const body = {
      channel: this._offer.channel,
      to: this._chips.to.map((r) => r.address),
      cc: this._chips.cc.map((r) => r.address),
      subject: this._subject,
      text: this._text,
    };
    if (this._account) body.account = this._account;
    // The same mail as the press that was never answered is that press
    // again; anything else is a new one.
    const sig = JSON.stringify(body);
    const key = this._press && this._press.sig === sig ? this._press.key : newRequestKey();
    this._press = { key, sig };
    body.request_key = key;
    this._busy = true;
    this._paintBusy();
    // Kept with its press before the request leaves: a page left now does
    // not come back with the same mail and a new key.
    this._saveLocal();
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
        saveDraft(this._key, null);
        this._press = null;
        this._busy = false;
        this._done = true;
        this._paintBusy();
        this._uninert();
        location.assign(`/chat.html?id=${encodeURIComponent(id)}`);
        return;
      }
      const said = doc && typeof doc.error === 'string' ? doc.error.trim().slice(0, 300) : '';
      if (res.status === 202 && doc && doc.unconfirmed === true) {
        error = 'The mail was handed over, but it is not confirmed yet that it went out. Look for it in the chat list' +
          (doc.request_key === key
            ? '; sending it again unchanged asks about this same mail and does not send it twice.'
            : ' before you send it again.');
      } else if (res.ok) {
        error = 'The server did not say where the new chat is — look for it in the chat list.';
      } else if ((res.status >= 400 && res.status < 500) || (doc && typeof doc === 'object' && doc.sent === false)) {
        // The server's word that nothing went out — its own refusal, or an
        // answer that says so ("sent": false: the mail server refused the
        // mail, the gateway was never reached). The press is over, and the
        // next one is a new one: a key that failed is not pressed again.
        this._press = null;
        error = said ? `Not sent: ${said}`
          : (res.status >= 500 ? `Not sent: the server could not send it (${res.status}).`
            : `Not sent: the server refused the request${res.status === 403 ? '' : ` (${res.status})`}.`);
      } else {
        error = `The server could not confirm this mail (${said || `it answered ${res.status}`}). ` +
          'If it shows up in the chat list, do not send it again.';
      }
    } catch (_err) {
      error = 'No answer came back, so it is not known whether the mail went out. ' +
        'If it shows up in the chat list, do not send it again.';
    }
    this._busy = false;
    this._errors.form = error;
    this._saveLocal();
    if (this.hasAttribute('hidden')) {
      this._show();
      return;
    }
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
