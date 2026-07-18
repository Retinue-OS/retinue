import { RetinueCard, esc } from './base.js';

class RetinueBriefing extends RetinueCard {
  css() {
    return `
      .b-title { font-weight: 600; margin: 0 0 6px; }
      .b-text { color: var(--fg, #e7ebf2); margin: 0 0 10px; white-space: pre-line; }
      audio { width: 100%; }
    `;
  }
  body(d) {
    const title = d.title ? `<p class="b-title">${esc(d.title)}</p>` : '';
    const text = d.text ? `<p class="b-text">${esc(d.text)}</p>` : '';
    const audio = d.audio
      ? `<audio controls preload="none" src="${esc(d.audio)}"></audio>`
      : '<p class="muted">Audio coming soon.</p>';
    if (!title && !text && !d.audio) return '<p class="muted">No briefing.</p>';
    return title + text + audio;
  }
}
customElements.define('retinue-briefing', RetinueBriefing);
