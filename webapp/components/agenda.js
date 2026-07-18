import { RetinueCard, esc } from './base.js';

class RetinueAgenda extends RetinueCard {
  css() {
    return `
      li { display: flex; gap: 12px; align-items: baseline; }
      .time { font-variant-numeric: tabular-nums; color: var(--accent, #6ea8fe); font-weight: 600; min-width: 3.2em; }
    `;
  }
  body(d) {
    const events = Array.isArray(d.events) ? d.events : [];
    if (!events.length) return '<p class="muted">No appointments today.</p>';
    return '<ul class="list">' + events.map((e) =>
      `<li><span class="time">${esc(e.time || '')}</span>` +
      `<span><strong>${esc(e.title || '')}</strong>` +
      (e.location ? `<small>${esc(e.location)}</small>` : '') +
      `</span></li>`
    ).join('') + '</ul>';
  }
}
customElements.define('retinue-agenda', RetinueAgenda);
