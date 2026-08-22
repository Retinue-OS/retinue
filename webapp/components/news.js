// News card and news page: broadcast-style inbound, ranked by how much it
// matters to you right now, every item a link to where it was published.
//
// The element reads the gateway's live endpoint:
//   GET /news?scope=feed&limit=n  ->  { generated, items: [...] }
// which ranks at read time from one number per item (importance, decayed by
// age; dated items hold their weight until they lapse and then drop out). There
// is no stored ordering to go stale — the feed re-ranks itself simply because
// the clock moved.
//
// Two modes, like projects.js:
//  - default: a compact dashboard card, top items only, link to the full page.
//  - `full` (news.html): every item, filters (feed / read / hidden), read-aloud
//    for the whole feed, and the profile the Herald has learned — visible and
//    editable, because a ranking you cannot inspect is one you cannot trust.
//
// The three per-item actions are the entire learning loop from the user's side:
//    👍 more like this   👎 less like this   ✕ not interested
// Each one nudges that item immediately and is logged for the Herald, which
// generalizes it into the profile on its next run (scripts/news-curate.py).

import {
  esc, fmtAge, isWideFrame, onFrameChange,
  viewPref, setViewPref, viewToggleHtml, VIEW_TOGGLE_CSS,
} from './base.js';
import { renderMarkdown, MD_CSS } from './markdown.js';
import { Reader, speechAvailable } from './speech.js';

const SRC = '/news';
const PREFS = '/news/preferences';
// The phone layout scrolls the whole page, so the card caps itself; in the wide
// layout the card scrolls inside its own column and shows everything it has.
const MAX_CARD_ITEMS = 5;

const CSS = `
  :host { display: block; }
  .card { padding: 2px; }
  @media (min-width: 700px) {
    .card {
      background: var(--card, #151922);
      border: 1px solid var(--line, rgba(231, 235, 242, .08));
      border-radius: var(--radius, 16px);
      padding: 14px 16px;
    }
  }
  header { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  h2 { font-size: .82rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
       color: var(--muted, #8b93a3); margin: 0 0 10px; }
  time { font-size: .72rem; color: var(--muted, #8b93a3); }
  .muted { color: var(--muted, #8b93a3); margin: 4px 0; }
  ul { list-style: none; margin: 0; padding: 0; display: grid; align-content: start;
       grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); gap: 8px; }
  /* The header's view toggle (base.js) forces a single full-width column. */
  ul.as-list { grid-template-columns: minmax(0, 1fr); }
  li { background: var(--card-2, #1c2230); border-radius: 10px; padding: 9px 11px;
       border-left: 3px solid transparent; }
  /* The top of the feed earns the accent bar; everything below is plain, so
     "what should I actually read" is answerable at a glance. */
  li.top { border-left-color: var(--accent, #6ea8fe); }
  li.reading { outline: 1px solid var(--accent, #6ea8fe); }
  li.done { opacity: .55; }
  a.headline { color: var(--fg, #e7ebf2); font-weight: 600; text-decoration: none;
       display: block; }
  a.headline:hover { text-decoration: underline; }
  .meta { display: block; color: var(--muted, #8b93a3); font-size: .78rem; margin-top: 3px; }
  .why { display: block; color: var(--fg2, #c3cad6); font-size: .8rem; margin-top: 3px;
       font-style: italic; }
  .summary { display: block; color: var(--fg2, #c3cad6); font-size: .84rem; margin-top: 4px; }
  .acts { display: flex; gap: 6px; margin-top: 7px; }
  button { font: inherit; font-size: .8rem; line-height: 1; cursor: pointer;
       background: var(--card, #151922); color: var(--fg2, #c3cad6);
       border: 1px solid var(--line, rgba(231, 235, 242, .12));
       border-radius: 8px; padding: 5px 8px; -webkit-tap-highlight-color: transparent; }
  button:hover { color: var(--fg, #e7ebf2); border-color: var(--accent, #6ea8fe); }
  button[aria-pressed="true"] { border-color: var(--accent, #6ea8fe); color: var(--fg, #e7ebf2); }
  .bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 10px; }
  .bar .spacer { flex: 1; }
  .more { display: inline-block; margin-top: 12px; color: var(--accent, #6ea8fe);
       text-decoration: none; font-size: .86rem; font-weight: 600; }
  .more:hover { text-decoration: underline; }
  details.prefs { margin-top: 16px; border-top: 1px solid var(--line, rgba(231, 235, 242, .08));
       padding-top: 12px; }
  details.prefs summary { cursor: pointer; color: var(--muted, #8b93a3); font-size: .82rem;
       font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }
  /* The "updated …" stamp rides inside the summary line, so it must opt out of
     the summary's own uppercase heading treatment. */
  details.prefs summary .stamp { text-transform: none; letter-spacing: 0;
       font-weight: 400; font-size: .78rem; }
  .prefs-body { margin-top: 10px; }
  textarea { width: 100%; box-sizing: border-box; min-height: 220px; font: inherit;
       font-size: .86rem; background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2);
       border: 1px solid var(--line, rgba(231, 235, 242, .12)); border-radius: 10px;
       padding: 8px 10px; }
  input.note { flex: 1; min-width: 160px; font: inherit; font-size: .84rem;
       background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2);
       border: 1px solid var(--line, rgba(231, 235, 242, .12)); border-radius: 8px;
       padding: 6px 9px; }
  ${MD_CSS}
`;

function ageLabel(item) {
  const age = fmtAge(item.published || item.fetched);
  return age || '';
}

function itemLi(item, opts) {
  const meta = [];
  if (item.source) meta.push(esc(item.source));
  const age = ageLabel(item);
  if (age) meta.push(esc(age));
  if (item.expires) meta.push(`until ${esc(String(item.expires).slice(0, 10))}`);
  const cls = [
    opts.top ? 'top' : '',
    item.read ? 'done' : '',
    opts.reading ? 'reading' : '',
  ].filter(Boolean).join(' ');
  const why = item.reason ? `<span class="why">${esc(item.reason)}</span>` : '';
  const summary = opts.summary && item.summary
    ? `<span class="summary">${esc(item.summary)}</span>` : '';
  // target=_blank so the dashboard (a PWA, often the only open window) is not
  // navigated away from; rel guards the opener.
  return `<li class="${cls}" data-id="${esc(item.id)}">` +
    `<a class="headline" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer"` +
    ` data-act="read" data-id="${esc(item.id)}">${esc(item.title)}</a>` +
    `<span class="meta">${meta.join(' · ')}</span>${why}${summary}` +
    `<div class="acts">` +
    `<button data-act="up" data-id="${esc(item.id)}" title="More like this">&#128077;</button>` +
    `<button data-act="down" data-id="${esc(item.id)}" title="Less like this">&#128078;</button>` +
    `<button data-act="hide" data-id="${esc(item.id)}" title="Not interested">&#10005;</button>` +
    (opts.speech
      ? `<button data-act="say" data-id="${esc(item.id)}" title="Read aloud">&#128266;</button>`
      : '') +
    `</div></li>`;
}

class RetinueNews extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._state = { state: 'loading' };
    this._scope = 'feed';
    this._prefs = null;
    this._editing = false;
    // List rows vs reflowing tiles — a per-device choice (see base.js).
    this._view = viewPref('news');
    this._reader = new Reader((id) => this._markReading(id));
    this.render();
    this.load();
    this.shadowRoot.addEventListener('click', (e) => this.onClick(e));
    this._offFrame = onFrameChange(() => { if (!this.full) this.render(); });
  }

  disconnectedCallback() {
    if (this._offFrame) this._offFrame();
    this._offFrame = null;
    this._reader.stop();
  }

  get full() { return this.hasAttribute('full'); }
  get heading() { return this.getAttribute('heading') || 'News'; }

  async load() {
    try {
      const res = await fetch(`${SRC}?scope=${encodeURIComponent(this._scope)}`,
        { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      this._state = { state: 'ok', data: await res.json() };
    } catch (_err) {
      this._state = { state: 'offline' };
    }
    this.render();
    if (this.full && this._prefs === null) this.loadPrefs();
  }

  async loadPrefs() {
    try {
      const res = await fetch(PREFS, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      this._prefs = await res.json();
    } catch (_err) {
      this._prefs = { markdown: '', error: true };
    }
    this.render();
  }

  get items() {
    const data = this._state.data;
    return data && Array.isArray(data.items) ? data.items : [];
  }

  // ── user actions ───────────────────────────────────────────────────────────

  async signal(id, sig, note) {
    const body = { signal: sig };
    if (id) body.id = id;
    if (note) body.note = note;
    try {
      await fetch('/news/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (_err) {
      // Offline: the tap is lost, which is the honest outcome — the feed is a
      // read-mostly surface and there is nothing to queue against a store the
      // client cannot reach.
      return;
    }
    await this.load();
  }

  onClick(e) {
    const vt = e.target.closest('[data-setview]');
    if (vt) {
      this._view = vt.getAttribute('data-setview');
      setViewPref('news', this._view);
      this.render();
      return;
    }
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    const id = el.dataset.id;
    if (act === 'read') {
      // Let the link open; just record that it was opened.
      this.signal(id, 'read');
      return;
    }
    e.preventDefault();
    if (act === 'up' || act === 'down' || act === 'hide') { this.signal(id, act); return; }
    if (act === 'say') { this.speak(this.items.filter((i) => i.id === id)); return; }
    if (act === 'listen') { this.speak(this.items); return; }
    if (act === 'stop') { this._reader.stop(); this.render(); return; }
    if (act === 'skip') { this._reader.skip(); return; }
    if (act === 'scope') { this._scope = el.dataset.scope; this.load(); return; }
    if (act === 'note') { this.submitNote(); return; }
    if (act === 'edit-prefs') { this._editing = true; this.render(); return; }
    if (act === 'cancel-prefs') { this._editing = false; this.render(); return; }
    if (act === 'save-prefs') { this.savePrefs(); return; }
  }

  submitNote() {
    const input = this.shadowRoot.querySelector('input.note');
    const text = input && input.value.trim();
    if (!text) return;
    input.value = '';
    this.signal(null, 'note', text);
  }

  async savePrefs() {
    const box = this.shadowRoot.querySelector('textarea');
    if (!box) return;
    try {
      const res = await fetch(PREFS, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ markdown: box.value }),
      });
      if (res.ok) this._prefs = await res.json();
    } catch (_err) { /* keep the editor open with the user's text */ return; }
    this._editing = false;
    this.render();
  }

  speak(items) {
    const pieces = items.map((i) => ({
      id: i.id,
      lang: i.lang,
      text: [i.title, i.source ? `— ${i.source}.` : '', i.summary || ''].join(' '),
    }));
    this._reader.play(pieces);
    this.render();
  }

  // Highlight the item being read without re-rendering the whole list (a
  // re-render mid-speech would drop the user's scroll position every sentence).
  _markReading(id) {
    const root = this.shadowRoot;
    if (!root) return;
    root.querySelectorAll('li.reading').forEach((li) => li.classList.remove('reading'));
    if (id) {
      // Item ids are hex digests, so they need no selector escaping.
      const li = root.querySelector(`li[data-id="${id}"]`);
      if (li) {
        li.classList.add('reading');
        li.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
      return;
    }
    // Reading finished (or was stopped): swap the transport buttons back.
    if (this.isConnected) this.render();
  }

  // ── rendering ─────────────────────────────────────────────────────────────

  render() {
    const { state, data } = this._state;
    // A deployment with no news sources declared has nothing to say here, and an
    // empty card is pure noise on a phone screen — so the card takes itself off
    // the dashboard until there is something in the feed. The dock button and
    // /news.html stay, so the page is still discoverable.
    if (!this.full) {
      this.hidden = state === 'ok' && !this.items.length;
      if (this.hidden) { this.shadowRoot.innerHTML = ''; return; }
    }
    let inner = '';
    let stamp = '';
    if (state === 'loading') {
      inner = '<p class="muted">&#8230;</p>';
    } else if (state === 'offline') {
      inner = '<p class="muted">Offline &ndash; no current news.</p>';
    } else {
      inner = this.full ? this.bodyFull() : this.bodyCard();
      if (data && data.generated) stamp = `<time>${esc(fmtAge(data.generated))}</time>`;
    }
    const home = this.full
      ? '<a class="more" href="/">&larr; Back to dashboard</a>' : '';
    this.shadowRoot.innerHTML =
      `<style>${CSS}${VIEW_TOGGLE_CSS}</style>` +
      `<section class="card"><header><h2>${esc(this.heading)}</h2>` +
      `${viewToggleHtml(this._view)}${stamp}</header>` +
      `<div class="content">${inner}</div>${home}</section>`;
  }

  get _ulOpen() { return this._view === 'list' ? '<ul class="as-list">' : '<ul>'; }

  bodyCard() {
    const items = this.items;
    if (!items.length) return '<p class="muted">Nothing in the feed.</p>';
    const shown = isWideFrame() ? items : items.slice(0, MAX_CARD_ITEMS);
    return this._ulOpen +
      shown.map((it, idx) => itemLi(it, {
        top: idx === 0, summary: false, speech: false,
        reading: this._reader.currentId === it.id,
      })).join('') +
      '</ul><a class="more" href="/news.html">All news &rarr;</a>';
  }

  bodyFull() {
    const items = this.items;
    const speech = speechAvailable();
    const scopeBtn = (scope, label) =>
      `<button data-act="scope" data-scope="${scope}" ` +
      `aria-pressed="${this._scope === scope}">${label}</button>`;
    const listenBtns = !speech ? '' : (this._reader.speaking
      ? '<button data-act="skip" title="Next item">&#9197;</button>' +
        '<button data-act="stop" title="Stop">&#9209;</button>'
      : '<button data-act="listen" title="Read the feed aloud">&#128266; Listen</button>');
    const bar =
      `<div class="bar">${scopeBtn('feed', 'Feed')}${scopeBtn('read', 'Read')}` +
      `${scopeBtn('hidden', 'Hidden')}<span class="spacer"></span>${listenBtns}</div>`;
    // An empty feed is ambiguous — read everything, or no sources declared? Say
    // which, and where sources come from, rather than leaving a blank page.
    const empty = this._scope === 'feed'
      ? '<p class="muted">Nothing in the feed. Sources are declared per chamber ' +
        'in <code>.news.json</code>; anything that is not a feed goes in with ' +
        '<code>news-add.py</code>.</p>'
      : '<p class="muted">Nothing here.</p>';
    const list = items.length
      ? this._ulOpen + items.map((it, idx) => itemLi(it, {
          top: idx === 0 && this._scope === 'feed', summary: true, speech,
          reading: this._reader.currentId === it.id,
        })).join('') + '</ul>'
      : empty;
    const note =
      '<div class="bar" style="margin-top:12px">' +
      '<input class="note" type="text" placeholder="Tell Herald what you want more or less of…">' +
      '<button data-act="note">Send</button></div>';
    return bar + list + note + this.prefsPanel();
  }

  prefsPanel() {
    const p = this._prefs;
    if (!p) return '';
    const body = this._editing
      ? `<textarea>${esc(p.markdown || '')}</textarea>` +
        '<div class="bar" style="margin-top:8px">' +
        '<button data-act="save-prefs">Save</button>' +
        '<button data-act="cancel-prefs">Cancel</button></div>'
      : `<div class="md">${renderMarkdown(p.markdown || '_Nothing learned yet._')}</div>` +
        '<div class="bar" style="margin-top:8px">' +
        '<button data-act="edit-prefs">Edit</button></div>';
    const stamp = p.updated ? ` <span class="stamp">— updated ${esc(fmtAge(p.updated))}</span>` : '';
    return `<details class="prefs"><summary>What Herald knows about your taste${stamp}</summary>` +
      `<div class="prefs-body">${body}</div></details>`;
  }
}
customElements.define('retinue-news', RetinueNews);
