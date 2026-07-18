import { RetinueCard, esc } from './base.js';

class RetinueMessages extends RetinueCard {
  css() {
    return `
      li { display: flex; gap: 10px; align-items: baseline; }
      .dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; margin-top: 6px;
             background: var(--muted, #8b93a3); }
      .dot.high { background: var(--high, #ff6b6b); }
      .who { font-weight: 600; }
      .chan { color: var(--muted, #8b93a3); font-size: .78rem; }
    `;
  }
  body(d) {
    const items = Array.isArray(d.items) ? d.items : [];
    if (!items.length) return '<p class="muted">Nothing important.</p>';
    return '<ul class="list">' + items.map((m) =>
      `<li><span class="dot ${m.importance === 'high' ? 'high' : ''}"></span>` +
      `<span><span class="who">${esc(m.from || '')}</span> ` +
      `<span class="chan">${esc(m.channel || '')}</span>` +
      `<small>${esc(m.preview || '')}</small></span></li>`
    ).join('') + '</ul>';
  }
}
customElements.define('retinue-messages', RetinueMessages);
