// Shared base for Retinue dashboard cards.
//
// A card fetches one JSON document (its `src` attribute), renders it inside a
// styled <section>, and degrades gracefully: it shows a relative timestamp from
// the document's `generated` field and falls back to "offline" when the fetch
// fails (the service worker still serves the last cached copy when possible).
//
// Subclasses override body(data) -> HTML string, and optionally css() -> extra
// CSS string.

export function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function fmtAge(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const mins = Math.round((Date.now() - t) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  return `${Math.round(hrs / 24)} d ago`;
}

// The dashboard has two layouts (see styles.css — keep this query in sync):
//  - the phone layout, where the PAGE scrolls and every card contributes its
//    full height to it, so a card must cap its list or the dashboard becomes an
//    endless scroll;
//  - the wide layout, a fixed app frame whose columns scroll internally, where a
//    card's list is bounded by its own scroll box. There a cap only wastes the
//    space it was meant to protect, so cards show everything they have.
export const WIDE_FRAME = '(min-width: 1000px) and (min-height: 480px)';

export function isWideFrame() {
  return typeof matchMedia === 'function' && matchMedia(WIDE_FRAME).matches;
}

// Run `fn` whenever the layout flips between the two modes (window resize,
// device rotation). Returns an unsubscribe function for disconnectedCallback.
export function onFrameChange(fn) {
  if (typeof matchMedia !== 'function') return () => {};
  const mq = matchMedia(WIDE_FRAME);
  const handler = () => fn(mq.matches);
  mq.addEventListener('change', handler);
  return () => mq.removeEventListener('change', handler);
}

// ── List/cards view preference ────────────────────────────────────────────────
// Each list card (conversations, projects, news) can present its rows either as
// reflowing tiles ("cards") or as a single full-width column ("list"). The
// choice is a per-device preference, persisted in localStorage per card key.
// On phones the tile grid collapses to one column anyway, so the toggle is only
// rendered in the wide layout (see VIEW_TOGGLE_CSS) — small devices keep the
// one presentation that makes sense there.
export function viewPref(key) {
  try { return localStorage.getItem(`retinue.view.${key}`) || 'cards'; }
  catch (_e) { return 'cards'; }
}

export function setViewPref(key, view) {
  try { localStorage.setItem(`retinue.view.${key}`, view); } catch (_e) { /* private mode */ }
}

// The header control both list cards share. Clicks carry data-setview; the
// component wires them (setViewPref + re-render). Glyphs, not words, so the
// control stays discrete next to the card heading.
export function viewToggleHtml(view) {
  const btn = (v, glyph, label) =>
    `<button type="button" class="vt${view === v ? ' on' : ''}" data-setview="${v}" ` +
    `title="${label}" aria-label="${label}" aria-pressed="${view === v}">${glyph}</button>`;
  return `<span class="viewtoggle">${btn('list', '&#9776;', 'Show as list')}` +
    `${btn('cards', '&#8862;', 'Show as cards')}</span>`;
}

export const VIEW_TOGGLE_CSS = `
  .viewtoggle { display: none; margin-left: auto; gap: 2px; }
  @media ${WIDE_FRAME} {
    .viewtoggle { display: inline-flex; }
  }
  .viewtoggle .vt { font: inherit; font-size: .78rem; line-height: 1; cursor: pointer;
    background: transparent; color: var(--muted, #8b93a3);
    border: 1px solid transparent; border-radius: 6px; padding: 3px 6px;
    -webkit-tap-highlight-color: transparent; }
  .viewtoggle .vt:hover { color: var(--fg, #e7ebf2); }
  .viewtoggle .vt.on { color: var(--accent, #6ea8fe);
    border-color: var(--line, rgba(231, 235, 242, .12)); background: var(--card-2, #1c2230); }
`;

const CARD_CSS = `
  :host { display: block; }
  .card {
    background: var(--card, #151922);
    border-radius: var(--radius, 16px);
    padding: 14px 16px;
  }
  header { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  h2 { font-size: .82rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
       color: var(--muted, #8b93a3); margin: 0 0 10px; }
  time { font-size: .72rem; color: var(--muted, #8b93a3); }
  .content { color: var(--fg, #e7ebf2); }
  .muted { color: var(--muted, #8b93a3); margin: 4px 0; }
  ul.list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
  small { display: block; color: var(--muted, #8b93a3); font-size: .8rem; }
`;

export class RetinueCard extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this.renderState({ state: 'loading' });
    this.load();
  }

  get dataUrl() { return this.getAttribute('src'); }
  get heading() { return this.getAttribute('heading') || ''; }

  async load() {
    if (!this.dataUrl) { this.renderState({ state: 'ok', data: {} }); return; }
    try {
      const res = await fetch(this.dataUrl, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      this.renderState({ state: 'ok', data });
    } catch (_err) {
      this.renderState({ state: 'offline' });
    }
  }

  // Override in subclasses.
  body(_data) { return ''; }
  css() { return ''; }

  renderState({ state, data }) {
    let inner;
    let stamp = '';
    if (state === 'loading') {
      inner = '<p class="muted">&#8230;</p>';
    } else if (state === 'offline') {
      inner = '<p class="muted">Offline &ndash; no current data.</p>';
    } else {
      inner = this.body(data || {});
      if (data && data.generated) stamp = `<time>${esc(fmtAge(data.generated))}</time>`;
    }
    this.shadowRoot.innerHTML =
      `<style>${CARD_CSS}${this.css()}</style>` +
      `<section class="card"><header><h2>${esc(this.heading)}</h2>${stamp}</header>` +
      `<div class="content">${inner}</div></section>`;
  }
}
