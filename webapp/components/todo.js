import { RetinueCard, esc } from './base.js';

class RetinueTodo extends RetinueCard {
  css() {
    return `
      .top-todo { font-size: 1.15rem; font-weight: 600; margin: 0 0 8px; }
      .others { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
      .others li { color: var(--muted, #8b93a3); }
    `;
  }
  body(d) {
    const top = d.top && d.top.title ? `<p class="top-todo">${esc(d.top.title)}</p>` : '';
    const others = Array.isArray(d.others) ? d.others : [];
    const rest = others.length
      ? '<ul class="others">' + others.map((o) => `<li>${esc(o.title || o)}</li>`).join('') + '</ul>'
      : '';
    if (!top && !rest) return '<p class="muted">Nothing open.</p>';
    return top + rest;
  }
}
customElements.define('retinue-todo', RetinueTodo);
