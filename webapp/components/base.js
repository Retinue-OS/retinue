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
