// Projects card: running projects, with the ones where you are on the move
// highlighted — the same shape as the conversations card.
//
// Unlike the mock data cards, this element does not read a static JSON file. It
// queries the gateway's live endpoint:
//   GET /projects  ->  { generated, mine: [...], waiting: [...] }
// which the gateway computes on demand with a SPARQL query over the "life"
// triple store. The project frontmatter (type/currentActor/expectedBy/…) is the
// single source of truth; qlever-dir's Markdown converter indexes it as triples,
// so there is no second representation to keep in sync.
//
// Two modes, like conversations.js:
//  - default: a compact dashboard card. "Your move" projects first, each with an
//    accent bar; then a short "Waiting on others" tail, capped, plus a link to
//    the dedicated page.
//  - `full` (used on projects.html): every project, no cap, grouped into
//    "Your move" and "Waiting on others".
//
// Degrades gracefully offline: the fetch fails, the last rendered state stays.

import {
  esc, fmtAge, isWideFrame, onFrameChange,
  viewPref, setViewPref, viewToggleHtml, VIEW_TOGGLE_CSS,
} from './base.js';

const SRC = '/projects';
// Caps for the dashboard card in the phone layout, where every row lengthens the
// scrolling page. In the wide layout the card scrolls inside its own column, so
// they are lifted there and the column shows the full picture.
const MAX_CARD_MINE = 6;
const MAX_CARD_WAITING = 3;

const CSS = `
  :host { display: block; }
  /* Chrome-less on phones (edge-to-edge, matching the conversations region);
     a framed card again on wide screens — same breakpoint as conversations. */
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
  .group-label { font-size: .72rem; font-weight: 600; letter-spacing: .04em;
       text-transform: uppercase; color: var(--muted, #8b93a3); margin: 14px 0 8px; }
  .group-label:first-of-type { margin-top: 2px; }
  /* Rows reflow into as many columns as fit — one in the dashboard's narrow
     projects column or on a phone (min(100%, …) collapses the track), several on
     the full projects page, where a single stretched row per project would leave
     most of a wide window empty. */
  ul { list-style: none; margin: 0; padding: 0; display: grid; align-content: start;
       grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); gap: 8px; }
  /* The header's view toggle (base.js) forces a single full-width column. */
  ul.as-list { grid-template-columns: minmax(0, 1fr); }
  /* Each row links to the project's own page (view, edit, discuss). Rows are
     tap targets, not prose: suppress text selection and the iOS long-press
     callout so a finger resting on a row while scrolling never starts a
     selection drag instead of the scroll. */
  li a { display: block; padding: 8px 10px; border-radius: 10px;
         background: var(--card-2, #1c2230); text-decoration: none;
         -webkit-tap-highlight-color: transparent;
         user-select: none; -webkit-user-select: none;
         -webkit-touch-callout: none; touch-action: manipulation; }
  /* Hover affordance only where a hover pointer exists — on touch screens the
     sticky :hover outline reads as the row being "selected". */
  @media (hover: hover) {
    li a:hover { outline: 1px solid var(--accent, #6ea8fe); }
  }
  /* "Your move": accent bar on the left, like an unread/active marker. */
  li.mine a { border-left: 3px solid var(--accent, #6ea8fe); }
  li.waiting a { border-left: 3px solid transparent; }
  li.waiting { opacity: .92; }
  .title { color: var(--fg, #e7ebf2); font-weight: 600; }
  .meta { display: block; color: var(--muted, #8b93a3); font-size: .8rem; margin-top: 2px; }
  .next { display: block; color: var(--fg2, #c3cad6); font-size: .84rem; margin-top: 3px; }
  .more { display: inline-block; margin-top: 12px; color: var(--accent, #6ea8fe);
       text-decoration: none; font-size: .86rem; font-weight: 600; }
  .more:hover { text-decoration: underline; }
`;

function projectLi(p, cls, opts = {}) {
  const bits = [];
  if (opts.next && p.next) bits.push(`<span class="next">${esc(p.next)}</span>`);
  const meta = [];
  if (cls === 'waiting') {
    meta.push(p.waitingOn ? `Waiting on ${esc(p.waitingOn)}` : 'Waiting');
    if (p.since) meta.push(`since ${esc(fmtAge(p.since) || p.since)}`);
  } else if (p.expected) {
    meta.push(`Due ${esc(p.expected)}`);
  }
  if (meta.length) bits.push(`<span class="meta">${meta.join(' · ')}</span>`);
  const href = `/project.html?id=${encodeURIComponent(p.id || '')}`;
  return `<li class="${cls}"><a href="${esc(href)}">` +
    `<span class="title">${esc(p.title)}</span>${bits.join('')}</a></li>`;
}

class RetinueProjects extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._last = { state: 'loading' };
    // List rows vs reflowing tiles — a per-device choice (see base.js). The
    // listener sits on the shadow root, so it survives every innerHTML swap.
    this._view = viewPref('projects');
    this.shadowRoot.addEventListener('click', (e) => {
      const el = e.target.closest('[data-setview]');
      if (!el) return;
      this._view = el.getAttribute('data-setview');
      setViewPref('projects', this._view);
      this.render(this._last);
    });
    this.render(this._last);
    this.load();
    // Crossing the layout breakpoint changes how many rows the card shows, so
    // re-render from the last data when it flips.
    this._offFrame = onFrameChange(() => { if (!this.full) this.render(this._last); });
  }

  disconnectedCallback() {
    if (this._offFrame) this._offFrame();
    this._offFrame = null;
  }

  get full() { return this.hasAttribute('full'); }
  get heading() { return this.getAttribute('heading') || 'Projects'; }

  async load() {
    try {
      const res = await fetch(SRC, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      this._last = { state: 'ok', data: await res.json() };
      this.render(this._last);
    } catch (_err) {
      this._last = { state: 'offline' };
      this.render(this._last);
    }
  }

  render({ state, data }) {
    let inner = '';
    let stamp = '';
    if (state === 'loading') {
      inner = '<p class="muted">&#8230;</p>';
    } else if (state === 'offline') {
      inner = '<p class="muted">Offline &ndash; no current data.</p>';
    } else {
      inner = this.full ? this.bodyFull(data) : this.bodyCard(data);
      if (data && data.generated) stamp = `<time>${esc(fmtAge(data.generated))}</time>`;
    }
    // On the dedicated page the component is all there is, so it must carry
    // the way home itself — in every state, including loading and offline
    // (inside the installed PWA there is no URL bar to fall back on).
    const home = this.full
      ? '<a class="more" href="/">&larr; Back to dashboard</a>' : '';
    this.shadowRoot.innerHTML =
      `<style>${CSS}${VIEW_TOGGLE_CSS}</style>` +
      `<section class="card"><header><h2>${esc(this.heading)}</h2>` +
      `${viewToggleHtml(this._view)}${stamp}</header>` +
      `<div class="content">${inner}</div>${home}</section>`;
  }

  get _ulOpen() { return this._view === 'list' ? '<ul class="as-list">' : '<ul>'; }

  bodyCard(d) {
    const mine = Array.isArray(d.mine) ? d.mine : [];
    const waiting = Array.isArray(d.waiting) ? d.waiting : [];
    if (!mine.length && !waiting.length) return '<p class="muted">No running projects.</p>';
    const wide = isWideFrame();
    const out = [];
    if (mine.length) {
      out.push(`<div class="group-label">Your move</div>${this._ulOpen}` +
        mine.slice(0, wide ? mine.length : MAX_CARD_MINE)
          .map((p) => projectLi(p, 'mine', { next: true })).join('') +
        '</ul>');
    }
    if (waiting.length) {
      out.push(`<div class="group-label">Waiting on others</div>${this._ulOpen}` +
        waiting.slice(0, wide ? waiting.length : MAX_CARD_WAITING)
          .map((p) => projectLi(p, 'waiting')).join('') +
        '</ul>');
    }
    out.push('<a class="more" href="/projects.html">All projects &rarr;</a>');
    return out.join('');
  }

  bodyFull(d) {
    const mine = Array.isArray(d.mine) ? d.mine : [];
    const waiting = Array.isArray(d.waiting) ? d.waiting : [];
    if (!mine.length && !waiting.length) return '<p class="muted">No running projects.</p>';
    const out = [];
    if (mine.length) {
      out.push(`<div class="group-label">Your move</div>${this._ulOpen}` +
        mine.map((p) => projectLi(p, 'mine', { next: true })).join('') + '</ul>');
    }
    if (waiting.length) {
      out.push(`<div class="group-label">Waiting on others</div>${this._ulOpen}` +
        waiting.map((p) => projectLi(p, 'waiting', { next: true })).join('') + '</ul>');
    }
    return out.join('');
  }
}
customElements.define('retinue-projects', RetinueProjects);
