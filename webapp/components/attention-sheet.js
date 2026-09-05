// The details sheet of an attention item — the three fields every item is
// judged by (docs/attention-model.md), each with its reason, each correctable
// in place, and the actions on the item: open it, Later, Pull, Mark done.
//
// One sheet for every surface: the home list opens it from a row's ⓘ, the
// thread bar and the chat page from theirs. It is a custom element appended to
// the page body on demand (a fixed overlay, so it floats over whatever page
// opened it) and talks to the gateway's attention API:
//   GET  /attention/item?id=…          the item as the list shows it, explained
//   POST /attention/items/<action>     later | pull | done | reopen | correct
//   POST /attention/permits            {sender, on}   a sender's permit in the mode in force
//   POST /attention/admit              {sphere, on}   a Focus rule of the mode in force
// A correction targets one field — importance, deadline, lead, sphere — and
// the gateway writes what it learned (a prior, a lead time) into the profile;
// the reply says so, and the sheet shows it. After every change the sheet
// dispatches `retinue-attention-change` on window, so an open list refreshes.

import { esc } from './base.js';

export const LEVEL_COLORS = {
  critical: '#ff5d5d', 'time-sensitive': '#e08a2e', active: '#4fb3b9', passive: '#4a5563',
};
export const SPHERE_COLORS = {
  customers: '#6ea8fe', admin: '#c9a0ff', health: '#ff6b6b', friends: '#57c785',
  family: '#ffb86b', system: '#9aa5b1',
};
export const sphereColor = (s) => SPHERE_COLORS[s] || '#9aa5b1';

const LEAD_PRESETS = [
  [60, '1 h'], [120, '2 h'], [360, '6 h'], [1440, '1 day'], [2880, '2 days'], [4320, '3 days'],
  [10080, '1 week'], [20160, '2 weeks'], [40320, '4 weeks'],
];

export function fmtDuration(minutes) {
  const m = Math.round(minutes);
  if (m < 60) return `${m} min`;
  if (m < 1440) { const h = m / 60; return `${Number.isInteger(h) ? h : h.toFixed(1)} h`; }
  const d = m / 1440;
  return `${Number.isInteger(d) ? d : d.toFixed(1)} d`;
}

// "Fri 17:00" today or tomorrow, the weekday within a week, else the date.
export function fmtWhen(iso) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '';
  const now = new Date();
  const dayOf = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((dayOf(t) - dayOf(now)) / 86400000);
  const hm = t.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (days === 0) return hm;
  if (days === 1) return `tomorrow ${hm}`;
  if (days > 1 && days < 7) return `${t.toLocaleDateString(undefined, { weekday: 'short' })} ${hm}`;
  return t.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) + ` ${hm}`;
}

export async function attentionAction(action, body) {
  const res = await fetch(`/attention/items/${action}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(String(res.status));
  const data = await res.json();
  window.dispatchEvent(new CustomEvent('retinue-attention-change', { detail: { action, id: body.id } }));
  return data;
}

export async function attentionRule(route, body) {
  const res = await fetch(`/attention/${route}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(String(res.status));
  const data = await res.json();
  window.dispatchEvent(new CustomEvent('retinue-attention-change', { detail: { action: route } }));
  return data;
}

// Open (or refresh) the sheet for one item. `opts.here` says the item's own
// view is the page the sheet floats over, so "Open" merely closes the sheet.
export function openAttentionSheet(id, opts = {}) {
  let el = document.querySelector('retinue-attention-sheet');
  if (!el) {
    el = document.createElement('retinue-attention-sheet');
    document.body.appendChild(el);
  }
  el.open(id, opts);
  return el;
}

const CSS = `
  :host { position: fixed; inset: 0; z-index: 50; display: block; }
  :host([hidden]) { display: none; }
  * { box-sizing: border-box; }
  button, select, input { font: inherit; }
  .overlay { position: absolute; inset: 0; background: rgba(0, 0, 0, .55);
             display: flex; align-items: flex-end; justify-content: center; }
  .sheet { background: var(--card, #151922); color: var(--fg, #e7ebf2);
           border-top: 1px solid var(--line, rgba(231, 235, 242, .12));
           border-radius: 20px 20px 0 0; width: 100%; max-width: 640px; max-height: 92vh;
           overflow: auto; padding: 14px 14px calc(env(safe-area-inset-bottom, 0px) + 18px);
           font: 15px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  @media (min-width: 700px) {
    .overlay { align-items: center; }
    .sheet { border-radius: 20px; border: 1px solid var(--line, rgba(231, 235, 242, .12)); }
  }
  .head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
  .title { font-weight: 700; font-size: 16px; }
  .src { color: var(--muted, #8b93a3); font-size: 12px; margin-top: 2px; }
  .x { color: var(--muted, #8b93a3); background: none; border: 0; font-size: 18px;
       cursor: pointer; padding: 0 4px; }
  .body { margin: 10px 0; color: #cbd3dd; font-size: 13.5px;
          border-left: 2px solid var(--line, rgba(231, 235, 242, .12)); padding-left: 10px; }
  .fields { display: flex; flex-direction: column; gap: 8px; margin: 10px 0; }
  .field { background: var(--bg, #0b0d12); border-radius: 10px; padding: 8px 10px; }
  .f-label { font-size: 10.5px; letter-spacing: 1.2px; color: var(--muted, #8b93a3);
             text-transform: uppercase; }
  .f-value { font-size: 13.5px; margin: 3px 0 6px; }
  .f-ctl { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; }
  .f-note { color: var(--muted, #8b93a3); font-size: 11px; }
  .f-k { color: var(--muted, #8b93a3); font-size: 11px; text-transform: uppercase;
         letter-spacing: .8px; }
  .f-break { flex-basis: 100%; height: 0; }
  .btn { font-size: 12.5px; padding: 5px 11px; border-radius: 8px; cursor: pointer;
         border: 1px solid var(--line, rgba(231, 235, 242, .12));
         background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); }
  .btn:hover { border-color: var(--accent, #6ea8fe); }
  .btn.primary { background: var(--accent, #6ea8fe); color: #0b0d12; border-color: var(--accent, #6ea8fe); }
  .btn.on { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }
  .btn.tiny { font-size: 12px; padding: 3px 8px; }
  .btn:disabled { opacity: .5; cursor: default; }
  .select, input[type="datetime-local"] { font-size: 12px; padding: 3px 6px; border-radius: 8px;
    border: 1px solid var(--line, rgba(231, 235, 242, .12));
    background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); color-scheme: dark; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .note { color: var(--muted, #8b93a3); font-size: 13px; margin: 6px 0; }
  .learned { margin-top: 10px; font-size: 12px; color: #c9a0ff; }
  .err { color: var(--high, #ff6b6b); font-size: 12.5px; margin-top: 8px; }
  .lvl { font-weight: 700; }
`;

class RetinueAttentionSheet extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._onKey = (e) => { if (e.key === 'Escape') this.close(); };
    this.shadowRoot.addEventListener('click', (e) => this._onClick(e));
    this.shadowRoot.addEventListener('change', (e) => this._onChange(e));
  }

  disconnectedCallback() { window.removeEventListener('keydown', this._onKey); }

  async open(id, opts = {}) {
    this._id = id;
    this._opts = opts;
    this._data = null;
    this._error = '';
    this._learned = [];
    this.hidden = false;
    window.addEventListener('keydown', this._onKey);
    this.render();
    await this.load();
  }

  close() {
    this.hidden = true;
    window.removeEventListener('keydown', this._onKey);
  }

  async load() {
    try {
      const res = await fetch(`/attention/item?id=${encodeURIComponent(this._id)}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(res.status === 404 ? 'This item is no longer on the list.' : `HTTP ${res.status}`);
      this._data = await res.json();
      this._error = '';
    } catch (err) {
      this._error = String((err && err.message) || err);
    }
    this.render();
  }

  async _act(action, extra = {}) {
    if (this._busy) return;
    this._busy = true;
    this.render();
    try {
      const data = await attentionAction(action, { id: this._id, ...extra });
      this._data = data;
      this._learned = data.learned_now || [];
      this._effect = data.effect || null;
      this._error = '';
      if (action === 'later' || action === 'done') {
        // The item left the list (for now): the sheet has said what it will,
        // and the row it belonged to is gone.
        this.close();
      }
    } catch (err) {
      this._error = `Could not apply that (${String((err && err.message) || err)}).`;
    } finally {
      this._busy = false;
      this.render();
    }
  }

  async _rule(route, body) {
    if (this._busy) return;
    this._busy = true;
    this.render();
    try {
      await attentionRule(route, body);
      await this.load();
    } catch (err) {
      this._error = `Could not change that (${String((err && err.message) || err)}).`;
      this.render();
    } finally {
      this._busy = false;
      this.render();
    }
  }

  _onClick(e) {
    const el = e.target.closest('[data-act]');
    if (!el) {
      // A click on the dimmed backdrop closes; inside the sheet it does not.
      if (e.target.classList && e.target.classList.contains('overlay')) this.close();
      return;
    }
    const act = el.getAttribute('data-act');
    const item = this._data && this._data.item;
    switch (act) {
      case 'close': this.close(); break;
      case 'open':
        if (this._opts.here) { this.close(); break; }
        if (item && item.href) {
          if (item.kind === 'thread' && location.pathname === '/') {
            this.close();
            location.hash = item.href.replace(/^\/?#?/, '#');
          } else {
            location.href = item.href;
          }
        }
        break;
      case 'later': this._act('later', { when: el.getAttribute('data-when') || 'next' }); break;
      case 'pull': this._act('pull'); break;
      case 'done': this._act('done'); break;
      case 'reopen': this._act('reopen'); break;
      case 'imp': {
        if (!item) break;
        const v = Math.max(0, Math.min(5, Number(item.importance) + Number(el.getAttribute('data-delta'))));
        if (v !== Number(item.importance)) this._act('correct', { importance: v });
        break;
      }
      case 'due-shift': {
        if (!item) break;
        const base = item.due ? new Date(item.due) : new Date();
        base.setMinutes(base.getMinutes() + Number(el.getAttribute('data-delta')));
        this._act('correct', { due: base.toISOString() });
        break;
      }
      case 'critical': this._act('correct', { critical: el.getAttribute('data-on') === '1' }); break;
      case 'permit':
        if (item && item.sender) this._rule('permits', { sender: item.sender, on: !item.permit });
        break;
      case 'admit':
        if (item) this._rule('admit', { sphere: item.sphere, on: !item.admits_sphere });
        break;
      default: break;
    }
  }

  _onChange(e) {
    const el = e.target.closest('[data-set]');
    if (!el) return;
    const what = el.getAttribute('data-set');
    if (what === 'due') {
      if (el.value === 'pick') { this._picking = true; this.render(); return; }
      this._act('correct', { due: el.value === 'none' ? null : el.value });
    } else if (what === 'due-pick') {
      if (el.value) { this._picking = false; this._act('correct', { due: new Date(el.value).toISOString() }); }
    } else if (what === 'lead') {
      this._act('correct', { lead: Number(el.value) });
    } else if (what === 'sphere') {
      this._act('correct', { sphere: el.value });
    }
  }

  _dueOptions(item) {
    const now = new Date();
    const at = (days, hour) => {
      const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + days, hour, 0, 0, 0);
      return d;
    };
    const opts = [
      [null, 'no deadline'], [at(0, 17), 'today 17:00'], [at(1, 12), 'tomorrow 12:00'],
      [at(1, 17), 'tomorrow 17:00'], [at(3, 17), 'in 3 days'], [at(7, 17), 'in a week'],
      [at(14, 17), 'in two weeks'], [at(28, 17), 'in four weeks'],
    ].filter(([d]) => d == null || d > now);
    if (item.due) {
      const cur = new Date(item.due);
      if (!opts.some(([d]) => d && Math.abs(d - cur) < 60000)) opts.splice(1, 0, [cur, fmtWhen(item.due)]);
    }
    const isCur = (d) => (d == null ? !item.due : item.due && Math.abs(d - new Date(item.due)) < 60000);
    return opts.map(([d, l]) =>
      `<option value="${d == null ? 'none' : esc(d.toISOString())}"${isCur(d) ? ' selected' : ''}>${esc(l)}</option>`)
      .join('') + '<option value="pick">pick a date…</option>';
  }

  render() {
    const root = this.shadowRoot;
    if (!root) return;
    const d = this._data;
    const item = d && d.item;
    let inner;
    if (!item) {
      inner = `<div class="head"><div class="title">${this._error ? 'Attention' : '…'}</div>` +
        `<button class="x" data-act="close" aria-label="Close">✕</button></div>` +
        (this._error ? `<div class="err">${esc(this._error)}</div>` : '');
    } else {
      const mode = d.mode || {};
      const lvl = item.level;
      const src = item.kind === 'chat'
        ? `${esc(item.channel || 'chat')} · ${esc(item.sender || '')}${item.count > 1 ? ` · ${item.count} messages` : ''}`
        : item.kind === 'thread'
          ? `Thread${item.agent ? ` opened by ${esc(item.agent)}` : ''}`
          : `Project · ${item.actor === 'you' ? 'your move' : `parked on ${esc(item.actor)}`}`;
      const busy = this._busy ? ' disabled' : '';
      const leadSel = `<select class="select" data-set="lead"${busy}>` +
        LEAD_PRESETS.map(([v, l]) => `<option value="${v}"${Math.round(item.lead) === v ? ' selected' : ''}>${l}</option>`).join('') +
        (LEAD_PRESETS.some(([v]) => Math.round(item.lead) === v) ? '' : `<option value="${item.lead}" selected>${esc(fmtDuration(item.lead))}</option>`) +
        '</select>';
      const dueCtl = item.critical
        ? `<span class="f-note">critical is declared, not derived</span>` +
          `<button class="btn tiny" data-act="critical" data-on="0"${busy}>not critical after all</button>`
        : `<span class="f-k">deadline</span> <select class="select" data-set="due"${busy}>${this._dueOptions(item)}</select>` +
          (this._picking ? `<input type="datetime-local" data-set="due-pick">` : '') +
          (item.due ? `<button class="btn tiny" data-act="due-shift" data-delta="-1440"${busy}>−1 day</button>` +
            `<button class="btn tiny" data-act="due-shift" data-delta="1440"${busy}>+1 day</button>` : '') +
          `<span class="f-break"></span><span class="f-k">lead</span> ${leadSel}` +
          `<span class="f-note">${item.kind_label ? `the lead corrects every “${esc(item.kind_label)}”` : 'this item only — no kind declared'}</span>`;
      const spheres = (d.spheres || Object.keys(SPHERE_COLORS));
      const sphereSel = `<select class="select" data-set="sphere"${busy}>` +
        spheres.map((s) => `<option value="${esc(s)}"${s === item.sphere ? ' selected' : ''}>${esc(s)}</option>`).join('') +
        (spheres.includes(item.sphere) ? '' : `<option value="${esc(item.sphere)}" selected>${esc(item.sphere)}</option>`) +
        '</select>';
      const permitBtn = item.sender
        ? `<button class="btn tiny${item.permit ? ' on' : ''}" data-act="permit"${busy}>${item.permit
          ? `Revoke ${esc(item.sender)}’s ${esc(mode.name)} permit`
          : `Let ${esc(item.sender)} interrupt in ${esc(mode.name)}`}</button>`
        : '';
      const admitBtn = `<button class="btn tiny${item.admits_sphere ? ' on' : ''}" data-act="admit"${busy}>${item.admits_sphere
        ? `Stop admitting ${esc(item.sphere)} in ${esc(mode.name)}`
        : `Admit ${esc(item.sphere)} in ${esc(mode.name)}`}</button>`;
      const openLabel = item.kind === 'chat' ? 'Open the chat' : item.kind === 'thread' ? 'Open the thread' : 'Open the project';
      let actions;
      if (item.state !== 'open') {
        actions = `<div class="note">Handled.</div><div class="actions">` +
          `<button class="btn" data-act="open">${openLabel}</button>` +
          `<button class="btn" data-act="reopen"${busy}>Put it back on the list</button></div>`;
      } else if (item.actor !== 'you') {
        actions = `<div class="note">Parked on ${esc(item.actor)}${item.waiting_since ? ` since ${esc(fmtWhen(item.waiting_since))}` : ''}.</div>` +
          `<div class="actions"><button class="btn primary" data-act="open">${openLabel}</button>` +
          `<button class="btn" data-act="done"${busy}>Mark resolved</button></div>`;
      } else {
        actions = `<div class="actions"><button class="btn primary" data-act="open">${openLabel}</button>` +
          `<button class="btn" data-act="later" data-when="next"${busy}>Later · ${esc(fmtWhen(d.next_breakpoint))}</button>` +
          `<button class="btn" data-act="later" data-when="tomorrow"${busy}>Later · tomorrow</button>` +
          (!item.released ? `<button class="btn" data-act="pull"${busy}>Pull into the list now</button>` : '') +
          `<button class="btn" data-act="done"${busy}>${item.kind === 'chat' ? 'Mark handled' : 'Mark done'}</button></div>`;
      }
      const learned = (this._learned || []).length
        ? `<div class="learned">Learned: ${this._learned.map(esc).join(' · ')}</div>`
        : '';
      const effect = this._effect && this._effect.type === 'push'
        ? `<div class="learned">Pushed now — ${esc(this._effect.reason)}</div>` : '';
      inner = `<div class="head"><div><div class="title">${esc(item.title)}</div><div class="src">${src}</div></div>` +
        `<button class="x" data-act="close" aria-label="Close">✕</button></div>` +
        (item.preview ? `<div class="body">${esc(item.preview)}</div>` : '') +
        `<div class="fields">` +
        `<div class="field"><div class="f-label">Importance</div><div class="f-value">${esc(item.importance_text)}</div>` +
        `<div class="f-ctl"><button class="btn tiny" data-act="imp" data-delta="-1"${busy}>−</button>` +
        `<button class="btn tiny" data-act="imp" data-delta="1"${busy}>+</button>` +
        `<span class="f-note">corrects the prior for ${esc(item.sender || item.kind_label || item.kind)}</span></div></div>` +
        `<div class="field"><div class="f-label">Urgency</div><div class="f-value">${esc(item.urgency)}</div><div class="f-ctl">${dueCtl}</div></div>` +
        `<div class="field"><div class="f-label">Sphere</div><div class="f-value">` +
        `<span style="color:${sphereColor(item.sphere)}">${esc(item.sphere)}</span>${item.tags.length ? ` <span class="f-note">+ ${item.tags.map(esc).join(', ')}</span>` : ''}</div>` +
        `<div class="f-ctl">${sphereSel}<span class="f-note">${item.sender ? `remembered for ${esc(item.sender)}` : 'this item'}</span></div></div>` +
        `<div class="field"><div class="f-label">Delivery</div><div class="f-value">level <span class="lvl" style="color:${LEVEL_COLORS[lvl] || '#9aa5b1'}">${esc(lvl)}</span> · ${esc(item.delivery)}</div>` +
        `<div class="f-ctl">${permitBtn}${admitBtn}<span class="f-note">a Focus rule of ${esc(mode.name)} — importance untouched</span></div></div>` +
        `</div>${actions}${learned}${effect}` +
        (this._error ? `<div class="err">${esc(this._error)}</div>` : '');
    }
    root.innerHTML = `<style>${CSS}</style><div class="overlay"><div class="sheet" role="dialog" aria-modal="true">${inner}</div></div>`;
  }
}

customElements.define('retinue-attention-sheet', RetinueAttentionSheet);
