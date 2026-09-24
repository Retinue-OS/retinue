// The home screen: what wants attention, in one list.
//
// Threads with Ara, messenger chats and running projects are one kind of thing
// here — an item with an importance, a deadline against a lead time, a sphere,
// and a delivery the gateway decided (docs/attention-model.md): pushed, held
// for the next digest, or merely listed. This element renders what
// GET /attention hands it — the sections Now · Next · Held · Waiting, each row
// with its preview and its three fields explained, the mode in force and the
// next breakpoint — and posts the user's actions back:
//   POST /attention/mode              {mode} by hand, or {mode: null} to follow the schedule
//   POST /attention/modes             {mode, only_admitted} — whether this mode's list shows
//                                     only what it admits (the rest folds into "Not now")
//   (the sheet, components/attention-sheet.js, carries the per-item actions)
// Opening a row goes where it goes today: a thread opens in place (the
// conversations viewer on the same page answers the #conversation-<id> hash),
// a chat on its page, a project on its page. The list polls on the
// conversations cadence and refreshes at once after any action on the sheet.
//
// A digest push opens the home as /?digest=<its time>: what that digest
// released comes first, in a section of its own, most pressing first, until
// Done; afterwards the rows the latest digest brought keep a quiet marker
// (each row's `digest_at` against the payload's `last_digest`).

import { esc, fmtAge } from './base.js';
import {
  LEVEL_COLORS, sphereColor, fmtWhen, openAttentionSheet, attentionRule,
} from './attention-sheet.js';

const SRC = '/attention';
const POLL_MS = 5000;
// Modes are moods — how interruptible — from none (rest) to all (chores).
const MODE_COLORS = {
  rest: '#3a4250', flow: '#0f4f57', focused: '#2f8a90', chores: '#8a94a0', social: '#7a4f96',
};
const modeColor = (id) => MODE_COLORS[id] || '#6ea8fe';

function prefGet(key, fallback) {
  try { const v = localStorage.getItem(`retinue.attention.${key}`); return v == null ? fallback : v === '1'; }
  catch (_e) { return fallback; }
}
function prefSet(key, on) {
  try { localStorage.setItem(`retinue.attention.${key}`, on ? '1' : '0'); } catch (_e) { /* private mode */ }
}

const CSS = `
  :host { display: flex; flex-direction: column; min-height: 0; }
  * { box-sizing: border-box; }
  button { font: inherit; }
  button:focus-visible { outline: 2px solid var(--accent, #6ea8fe); outline-offset: 1px; }
  .card { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 2px; }
  @media (min-width: 700px) {
    .card { background: var(--card, #151922); border: 1px solid var(--line, rgba(231, 235, 242, .08));
            border-radius: var(--radius, 16px); padding: 14px 16px; }
  }
  /* <main> already pads the top safe-area inset for the whole page. */
  header { flex: none; display: flex; align-items: baseline; justify-content: space-between;
           gap: 8px; padding: 0 2px 10px; }
  /* The mode is the page's title: the state the user is in, in the biggest
     type on the screen, and the way to change it. */
  .mode-head { display: inline-flex; align-items: baseline; gap: 8px; min-width: 0; flex: 1 1 auto;
               background: none; border: 0; padding: 0; margin: 0; cursor: pointer;
               color: var(--fg, #e7ebf2); text-align: left;
               -webkit-tap-highlight-color: transparent; }
  /* The name is the one thing that must never truncate; a long scope title
     gives way after it, then the "until" note. */
  .mode-name { font-size: 1.3rem; font-weight: 650; letter-spacing: -.01em; white-space: nowrap;
               min-width: 0; overflow: hidden; text-overflow: ellipsis; flex: 0 1 auto; }
  .mode-subject { font-weight: 500; font-size: 1.05rem; }
  .subjects { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: -2px 0 10px 32px; }
  .scope-k { flex-basis: 100%; font-size: .68rem; letter-spacing: .08em; text-transform: uppercase;
             color: var(--muted, #8b93a3); margin: 2px 0 -2px; }
  /* A scope makes the title long; on a phone the date yields to it. */
  @media (max-width: 480px) { header.scoped .head-right .date { display: none; } }
  .subject { font-size: 12px; padding: 3px 9px; border-radius: 8px; cursor: pointer;
             border: 1px solid var(--line, rgba(231, 235, 242, .12));
             background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); }
  .subject.on { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }
  .mode-when { font-size: .8rem; color: var(--muted, #8b93a3); white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis; flex: 0 3 auto; min-width: 3em; }
  .caret { font-size: .7rem; color: var(--muted, #8b93a3); flex: none; }
  .head-right { display: inline-flex; align-items: baseline; gap: 12px; flex: none; }
  .head-right .date { color: var(--muted, #8b93a3); font-size: .8rem; white-space: nowrap; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; flex: none;
         align-self: center; }
  .content { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .list { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
  .sec { margin: 4px 0 12px; }
  .sec h4 { margin: 8px 4px 6px; font-size: .7rem; letter-spacing: .1em; text-transform: uppercase;
            color: var(--muted, #8b93a3); font-weight: 600; }
  .sec-toggle { width: 100%; font-size: .7rem; letter-spacing: .1em; text-transform: uppercase;
                color: var(--muted, #8b93a3); background: var(--card-2, #1c2230); border: 0;
                border-radius: 10px; padding: 10px 12px; display: flex; justify-content: space-between;
                cursor: pointer; margin: 6px 0; -webkit-tap-highlight-color: transparent; }
  .rows { display: grid; gap: 6px; align-content: start;
          grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); }
  .row { display: block; width: 100%; text-align: left; color: var(--fg, #e7ebf2);
         background: var(--card-2, #1c2230); border: 0; border-radius: 12px;
         padding: 10px 12px 10px 18px; cursor: pointer; position: relative;
         -webkit-tap-highlight-color: transparent; user-select: none; -webkit-user-select: none;
         touch-action: manipulation; }
  .row::before { content: ""; position: absolute; left: 8px; top: 10px; bottom: 10px; width: 3px;
                 border-radius: 2px; background: var(--stripe); }
  @media (hover: hover) { .row:hover { outline: 1px solid var(--accent, #6ea8fe); } }
  .row-top { display: flex; align-items: center; gap: 8px; font-size: .72rem; color: var(--muted, #8b93a3); }
  .row-top .meta { margin-left: auto; text-align: right; white-space: nowrap; overflow: hidden;
                   text-overflow: ellipsis; }
  .chip { display: inline-flex; align-items: center; gap: 5px; background: var(--bg, #0b0d12);
          color: #cbd3dd; border-radius: 8px; padding: 1px 8px; font-size: .72rem; }
  .chip i { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
  /* The small facts beside the sphere never wrap; the meta on the right
     gives way instead (ellipsis), so a row's first line stays one line. */
  .count { color: var(--muted, #8b93a3); white-space: nowrap; flex: none; }
  .row-top .meta { min-width: 0; }
  .row-title { font-size: .95rem; font-weight: 600; margin-top: 5px; display: flex; gap: 8px;
               align-items: baseline; }
  .row-title .t { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .info { flex: none; font-size: .95rem; color: var(--muted, #8b93a3); background: none; border: 0;
          cursor: pointer; padding: 0 2px; line-height: 1; }
  .info:hover { color: var(--accent, #6ea8fe); }
  .row-preview { font-size: .82rem; color: #cbd3dd; margin-top: 2px; white-space: nowrap;
                 overflow: hidden; text-overflow: ellipsis; }
  .row-why { font-size: .72rem; color: var(--muted, #8b93a3); margin-top: 3px; }
  .unread .row-title .t { color: var(--fg, #e7ebf2); }
  .pending .row-why::before { content: "Ara is working · "; color: var(--accent, #6ea8fe); }
  .empty { color: var(--muted, #8b93a3); text-align: center; padding: 40px 20px; }
  /* What the digest just released: first, framed, and dismissed with Done. */
  .sec.digest { background: color-mix(in srgb, var(--accent, #6ea8fe) 9%, transparent);
                border: 1px solid color-mix(in srgb, var(--accent, #6ea8fe) 35%, transparent);
                border-radius: 14px; padding: 2px 8px 8px; }
  .digest-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .digest-head h4 { color: var(--accent, #6ea8fe); }
  .digest-done { font-size: .78rem; padding: 4px 12px; border-radius: 999px; cursor: pointer;
                 border: 1px solid var(--accent, #6ea8fe); background: none; color: var(--accent, #6ea8fe); }
  .digest-empty { color: var(--muted, #8b93a3); font-size: .85rem; margin: 2px 4px 6px; }
  .from-digest { color: var(--accent, #6ea8fe); }
  .muted { color: var(--muted, #8b93a3); margin: 4px 0; }
  .degraded { color: var(--muted, #8b93a3); font-size: .78rem; margin: 2px 4px 6px; }
  .foot { flex: none; display: flex; flex-direction: column; gap: 10px; padding-top: 12px; }
  .new { width: 100%; padding: 12px; border-radius: 14px; border: 0; cursor: pointer;
         background: var(--accent, #6ea8fe); color: #0b0d12; font-weight: 600; }
  /* The mode menu: a fixed overlay so it floats over the whole page. */
  .overlay { position: fixed; inset: 0; z-index: 40; background: rgba(0, 0, 0, .55);
             display: flex; align-items: flex-end; justify-content: center; }
  .menu { background: var(--card, #151922); border-top: 1px solid var(--line, rgba(231, 235, 242, .12));
          border-radius: 20px 20px 0 0; width: 100%; max-width: 640px; max-height: 92vh; overflow: auto;
          padding: 14px 14px calc(env(safe-area-inset-bottom, 0px) + 18px); }
  @media (min-width: 700px) {
    .overlay { align-items: center; }
    .menu { border-radius: 20px; border: 1px solid var(--line, rgba(231, 235, 242, .12)); }
  }
  .menu-head { font-size: .7rem; letter-spacing: .1em; text-transform: uppercase;
               color: var(--muted, #8b93a3); margin: 4px 4px 8px; }
  .menu-row { display: flex; gap: 10px; align-items: center; width: 100%; text-align: left;
              color: var(--fg, #e7ebf2); background: var(--bg, #0b0d12); border: 1px solid transparent;
              border-radius: 10px; padding: 8px 10px; margin: 0 0 6px; cursor: pointer; }
  .menu-row.on { border-color: var(--accent, #6ea8fe); }
  .menu-row b { display: block; font-size: .95rem; }
  .menu-row small { color: var(--muted, #8b93a3); font-size: .8rem; }
  .menu-row.follow { margin-top: 10px; }
  .menu-note { color: var(--muted, #8b93a3); font-size: .78rem; margin: 8px 4px 0; }
  .menu-fold { display: flex; gap: 10px; align-items: flex-start; margin: 14px 4px 0; cursor: pointer;
               padding-top: 12px; border-top: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .menu-fold input { margin: 3px 0 0; accent-color: var(--accent, #6ea8fe); flex: none; }
  .menu-fold b { display: block; font-size: .9rem; font-weight: 600; }
  .menu-fold small { color: var(--muted, #8b93a3); font-size: .78rem; }
`;

class RetinueAttention extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._data = null;
    this._state = 'loading';
    this._menu = false;
    this._heldOpen = prefGet('held', false);
    this._waitingOpen = prefGet('waiting', false);
    this._notNowOpen = prefGet('not_now', false);
    this.shadowRoot.addEventListener('click', (e) => this._onClick(e));
    this._onChange = () => this.load();
    window.addEventListener('retinue-attention-change', this._onChange);
    this._onVisible = () => { if (document.visibilityState === 'visible') this.load(); };
    document.addEventListener('visibilitychange', this._onVisible);
    this.render();
    this.load();
    this._timer = setInterval(() => this.load(), POLL_MS);
    // A deep link straight to one item's sheet (?item=<id>) — the way to
    // look at a held item's reasons — or to what a digest released
    // (?digest=<its time>, the digest push's link).
    try {
      const params = new URLSearchParams(location.search);
      const item = params.get('item');
      if (item) openAttentionSheet(item);
      this._digest = params.get('digest') || null;
    } catch (_e) { /* no query */ }
  }

  disconnectedCallback() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
    window.removeEventListener('retinue-attention-change', this._onChange);
    document.removeEventListener('visibilitychange', this._onVisible);
  }

  // The list's name, for assistive tech only: the header's own words are the
  // mode, and a second "Attention" title above a page that is nothing but
  // the attention list was one line of chrome saying what the page already is.
  get heading() { return this.getAttribute('heading') || 'Attention'; }

  async load() {
    try {
      const res = await fetch(SRC, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const sig = JSON.stringify([data.sections, data.mode, data.next_breakpoint, data.degraded, data.last_digest]);
      this._data = data;
      this._state = 'ok';
      if (sig === this._sig && !this._menu) return;
      this._sig = sig;
      this.render();
    } catch (_err) {
      if (!this._data) { this._state = 'offline'; this.render(); }
    }
  }

  async _setFold(modeId, on) {
    try {
      const res = await fetch('/attention/modes', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: modeId, only_admitted: on }),
      });
      if (!res.ok) throw new Error(String(res.status));
      this._sig = '';
      window.dispatchEvent(new CustomEvent('retinue-attention-change', { detail: { action: 'rules' } }));
    } catch (_err) { /* the next poll shows the truth */ }
    await this.load();
  }

  // The projects on the list — as items, or as what a thread is about —
  // which is what one could be focusing on right now.
  _projects() {
    const seen = new Map();
    for (const r of this._rows()) {
      if (r.kind === 'project') seen.set(r.id, r.title);
      else if (r.project && !seen.has(r.project)) seen.set(r.project, r.project_title || r.project);
    }
    return [...seen].map(([id, title]) => ({ id, title }));
  }

  async _setMode(mode, subject, project) {
    this._menu = false;
    try {
      const res = await fetch('/attention/mode', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: mode || null, subject: subject || null, project: project || null }),
      });
      if (!res.ok) throw new Error(String(res.status));
      this._data = await res.json();
      this._sig = '';
      window.dispatchEvent(new CustomEvent('retinue-attention-change', { detail: { action: 'mode' } }));
    } catch (_err) { /* the next poll shows the truth */ }
    this.render();
  }

  _onClick(e) {
    const el = e.target.closest('[data-act]');
    if (!el) {
      if (e.target.classList && e.target.classList.contains('overlay')) { this._menu = false; this.render(); }
      return;
    }
    const act = el.getAttribute('data-act');
    const id = el.getAttribute('data-id');
    switch (act) {
      case 'open': this._openItem(id); break;
      case 'info': e.stopPropagation(); openAttentionSheet(id); break;
      case 'mode-menu': this._menu = !this._menu; this.render(); break;
      case 'set-mode': this._setMode(el.getAttribute('data-mode'), el.getAttribute('data-subject'), el.getAttribute('data-project')); break;
      case 'close-menu': this._menu = false; this.render(); break;
      case 'toggle-held': this._heldOpen = !this._heldOpen; prefSet('held', this._heldOpen); this.render(); break;
      case 'toggle-waiting': this._waitingOpen = !this._waitingOpen; prefSet('waiting', this._waitingOpen); this.render(); break;
      case 'toggle-not_now': this._notNowOpen = !this._notNowOpen; prefSet('not_now', this._notNowOpen); this.render(); break;
      case 'fold': this._setFold(el.getAttribute('data-mode'), el.getAttribute('data-on') === '1'); break;
      case 'new': location.hash = '#new'; break;
      case 'digest-done': this._closeDigest(); break;
      default: break;
    }
  }

  _openItem(id) {
    const row = this._rows().find((r) => r.id === id);
    if (!row || !row.href) return;
    if (row.kind === 'thread') {
      // In place: the conversations viewer on this page answers the hash.
      location.hash = row.href.replace(/^\/?#?/, '#');
      return;
    }
    location.href = row.href;
  }

  _rows() {
    const s = (this._data && this._data.sections) || {};
    return [].concat(s.now || [], s.next || [], s.held || [], s.waiting || [], s.not_now || []);
  }

  _rowHtml(r, section, inDigest = false) {
    const lvl = r.level;
    const meta = r.actor !== 'you'
      ? `waiting${r.waiting_since ? ` ${fmtAge(r.waiting_since).replace(' ago', '')}` : ''}`
      : `${r.due ? `due ${fmtWhen(r.due)}` : (r.critical ? 'critical' : 'no deadline')} · ${r.kind === 'chat' ? (r.channel || 'chat') : (r.project ? 'project' : r.kind)}`;
    const why = r.actor !== 'you'
      ? `importance ${r.importance} · parked on ${esc(r.actor)}`
      : `importance ${r.importance} · ${esc(section === 'now' ? r.reason : r.delivery)}`;
    const cls = `row${r.unread ? ' unread' : ''}${r.pending ? ' pending' : ''}`;
    return `<button class="${cls}" data-act="open" data-id="${esc(r.id)}" style="--stripe:${LEVEL_COLORS[lvl] || '#4a5563'}" title="${esc(lvl)}">` +
      `<div class="row-top"><span class="chip"><i style="background:${sphereColor(r.sphere)}"></i>${esc(r.sphere)}</span>` +
      (r.unknown_sender ? '<span class="count">new number</span>' : '') +
      (r.count > 1 ? `<span class="count">${r.count} msgs</span>` : '') +
      (!inDigest && this._fromLastDigest(r)
        ? `<span class="count from-digest" title="Released by the ${esc(fmtWhen(r.digest_at))} digest">digest ${esc(fmtWhen(r.digest_at))}</span>` : '') +
      `<span class="meta">${esc(meta)}</span></div>` +
      `<div class="row-title"><span class="t">${esc(r.title)}</span>` +
      `<span class="info" role="button" tabindex="0" data-act="info" data-id="${esc(r.id)}" title="Importance, urgency, delivery — and their corrections" aria-label="Details">ⓘ</span></div>` +
      (r.preview ? `<div class="row-preview">${esc(r.preview)}</div>` : '') +
      `<div class="row-why">${why}</div></button>`;
  }

  _fromLastDigest(r) {
    const last = this._data && this._data.last_digest;
    return !!(last && r.digest_at && r.digest_at === last.at);
  }

  // Leaving the digest view: the rows go back to their sections, and the
  // link's ?digest= goes from the address so a reload shows the plain home.
  _closeDigest() {
    this._digest = null;
    try {
      const url = new URL(location.href);
      url.searchParams.delete('digest');
      history.replaceState(history.state, '', url.pathname + url.search + url.hash);
    } catch (_e) { /* the view closes anyway */ }
    this.render();
  }

  // What the digest of `at` released and is still open, most pressing first
  // (the gateway's own order: level, importance, the nearest deadline), each
  // row with the section it would otherwise sit in — for its reason line.
  _digestRows(s, at) {
    const LEVEL = { critical: 0, 'time-sensitive': 1, active: 2, passive: 3 };
    const out = [];
    for (const key of ['now', 'next', 'not_now', 'held']) {
      for (const r of s[key] || []) if (r.digest_at === at) out.push({ r, key });
    }
    const due = (r) => (r.due ? Date.parse(r.due) : Infinity);
    out.sort((a, b) => (LEVEL[a.r.level] ?? 9) - (LEVEL[b.r.level] ?? 9)
      || b.r.importance - a.r.importance || due(a.r) - due(b.r));
    return out;
  }

  _digestHtml(rows) {
    const when = fmtWhen(this._digest);
    const inner = rows.length
      ? `<div class="rows">${rows.map(({ r, key }) => this._rowHtml(r, key, true)).join('')}</div>`
      : `<div class="digest-empty">Everything the ${esc(when)} digest brought is handled.</div>`;
    return `<section class="sec digest"><div class="digest-head"><h4>Digest ${esc(when)} · ${rows.length}</h4>` +
      `<button class="digest-done" data-act="digest-done">Done</button></div>${inner}</section>`;
  }

  _sectionHtml(label, items, key) {
    if (!items.length) return '';
    return `<section class="sec ${key}"><h4>${esc(label)} · ${items.length}</h4>` +
      `<div class="rows">${items.map((r) => this._rowHtml(r, key)).join('')}</div></section>`;
  }

  _collapsibleHtml(label, items, key, open) {
    if (!items.length) return '';
    return `<section class="sec ${key}"><button class="sec-toggle" data-act="toggle-${key}">` +
      `<span>${esc(label)} · ${items.length}</span><span>${open ? '▾' : '▸'}</span></button>` +
      (open ? `<div class="rows">${items.map((r) => this._rowHtml(r, key)).join('')}</div>` : '') +
      `</section>`;
  }

  // The home's whole header: the mode in force as the page's title — the
  // state the user is in, which is what the space above a list of what wants
  // attention is worth spending on — plus the date. The way to settings and
  // to the other pages is the navigation row above it (components/nav.js).
  // The date comes from the gateway's clock (`now`), not the browser's: it is
  // the clock every deadline on the list is read against.
  _headHtml(d) {
    const mode = (d && d.mode) || null;
    const date = new Date((d && d.now) || Date.now());
    const dateText = Number.isNaN(date.getTime()) ? ''
      : date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
    const right = `<span class="head-right"><span class="date">${esc(dateText)}</span></span>`;
    if (!mode) {
      return `<header><span class="mode-head"><span class="mode-when">` +
        `${this._state === 'offline' ? 'Offline' : '&#8230;'}</span></span>${right}</header>`;
    }
    const schedUntil = (mode.scheduled || {}).until;
    const until = mode.manual ? 'set by hand'
      : (schedUntil ? `until ${esc(fmtWhen(schedUntil))}` : '');
    const scope = mode.subject || null;
    const subject = scope
      ? ` <span class="mode-subject" style="color:${scope.kind === 'project' ? 'inherit' : sphereColor(scope.id)}">· ${esc(scope.title || scope.id)}</span>` : '';
    return `<header${scope ? ' class="scoped"' : ''}>` +
      `<button class="mode-head" data-act="mode-menu" aria-haspopup="dialog" ` +
      `title="${esc(mode.blurb || '')}">` +
      `<span class="dot" style="background:${modeColor(mode.id)}"></span>` +
      `<span class="mode-name">${esc(mode.name)}${subject}</span>` +
      `<span class="mode-when">${until}</span><span class="caret">&#9662;</span>` +
      `</button>${right}</header>`;
  }

  // Which day plan the schedule follows today — "Workday: ", or on a
  // holiday "Christmas · Day off: " — so the row says why it is what it is.
  _dayHtml(day) {
    if (!day || !day.plan) return '';
    return `${day.holiday ? `${esc(day.holiday)} · ` : ''}${esc(day.plan)}: `;
  }

  _menuHtml() {
    const d = this._data;
    if (!d) return '';
    const cur = d.mode;
    const spheres = (d.spheres || []).filter((x) => x !== 'unknown');
    const rows = (d.modes || []).map((m) => {
      const on = cur.id === m.id && cur.manual;
      let row = `<button class="menu-row${on ? ' on' : ''}" data-act="set-mode" data-mode="${esc(m.id)}">` +
        `<span class="dot" style="background:${modeColor(m.id)}"></span><span><b>${esc(m.name)}` +
        `${on && cur.subject ? ` · ${esc(cur.subject.title || cur.subject.id)}` : ''}</b><small>${esc(m.blurb)}</small></span></button>`;
      if (m.with_subject) {
        // The scope: a sphere ("all clients") or one of the projects on the
        // list ("this one"). The row itself enters the mode on nothing.
        const sub = (on && cur.subject) || {};
        const chip = (kind, id, title) => {
          const isOn = sub.kind === kind && sub.id === id;
          const attr = kind === 'project' ? `data-project="${esc(id)}"` : `data-subject="${esc(id)}"`;
          const color = kind === 'project' ? 'var(--muted, #8b93a3)' : sphereColor(id);
          return `<button class="subject${isOn ? ' on' : ''}" data-act="set-mode" data-mode="${esc(m.id)}" ${attr} ` +
            `style="border-color:${isOn ? '' : color}">${esc(title)}</button>`;
        };
        const projects = this._projects();
        row += `<div class="subjects"><span class="scope-k">on a sphere</span>` + spheres.map((x) => chip('sphere', x, x)).join('') + `</div>` +
          (projects.length ? `<div class="subjects"><span class="scope-k">on one project</span>` + projects.map((pr) => chip('project', pr.id, pr.title)).join('') + `</div>` : '');
      }
      return row;
    }).join('');
    const sch = cur.scheduled || {};
    return `<div class="overlay" data-act="close-menu"><div class="menu" role="dialog" aria-label="Focus mode">` +
      `<div class="menu-head">Focus mode</div>${rows}` +
      `<button class="menu-row follow${cur.manual ? '' : ' on'}" data-act="set-mode" data-mode="">` +
      `<span class="dot" style="background:${modeColor(sch.id)}"></span><span><b>Follow the schedule</b>` +
      `<small>${this._dayHtml(cur.day)}${esc(sch.name || '')}${sch.until ? ` until ${esc(fmtWhen(sch.until))}` : ''}</small></span></button>` +
      `<div class="menu-note">A change by hand is a breakpoint: what was held is released, and the digest goes out.</div>` +
      `<label class="menu-fold"><input type="checkbox" data-act="fold" data-mode="${esc(cur.id)}" data-on="${cur.only_admitted ? '0' : '1'}"${cur.only_admitted ? ' checked' : ''}>` +
      `<span><b>In ${esc(cur.name)}, list only what it admits</b><small>The rest folds into “Not now”. Critical, permitted and pulled items stay.</small></span></label>` +
      `</div></div>`;
  }

  render() {
    const root = this.shadowRoot;
    if (!root) return;
    let head = this._headHtml(null);
    let body;
    if (this._state === 'loading') {
      body = '<p class="muted">&#8230;</p>';
    } else if (this._state === 'offline' || !this._data) {
      body = '<p class="muted">Offline &ndash; no current data.</p>';
    } else {
      const d = this._data;
      let s = d.sections || {};
      const mode = d.mode || {};
      head = this._headHtml(d);
      // Opened from a digest push: what it released first, and not twice.
      let digest = '';
      if (this._digest) {
        const rows = this._digestRows(s, this._digest);
        const ids = new Set(rows.map(({ r }) => r.id));
        s = Object.fromEntries(Object.entries(s).map(([k, v]) => [k, (v || []).filter((r) => !ids.has(r.id))]));
        digest = this._digestHtml(rows);
      }
      const total = (s.now || []).length + (s.next || []).length + (s.held || []).length + (s.waiting || []).length + (s.not_now || []).length;
      const nb = fmtWhen(d.next_breakpoint);
      const degraded = (d.degraded || []).length
        ? `<div class="degraded">${esc(d.degraded.join(' and '))} unavailable right now — the life store is not answering.</div>` : '';
      body = `<div class="list">${degraded}${digest}` +
        this._sectionHtml('Now', s.now || [], 'now') +
        this._sectionHtml('Next', s.next || [], 'next') +
        this._collapsibleHtml(`Held until ${nb}`, s.held || [], 'held', this._heldOpen) +
        this._collapsibleHtml('Waiting on others', s.waiting || [], 'waiting', this._waitingOpen) +
        this._collapsibleHtml('Not now', s.not_now || [], 'not_now', this._notNowOpen) +
        (total || digest ? '' : '<div class="empty">Nothing wants your attention.</div>') +
        `</div>`;
    }
    // The one action the home offers beside the rows, within thumb reach;
    // the pages beside the home are in the navigation row at the top.
    const foot = `<div class="foot"><button class="new" data-act="new">+ Ask Ara</button></div>`;
    root.innerHTML = `<style>${CSS}</style><section class="card" aria-label="${esc(this.heading)}">${head}<div class="content">${body}${foot}</div></section>` +
      (this._menu ? this._menuHtml() : '');
  }
}

customElements.define('retinue-attention', RetinueAttention);
