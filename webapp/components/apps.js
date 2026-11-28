// The Apps page: the apps the mounted chambers ship (docs/chamber-apps.md).
//
//   GET /apps/index.json -> {apps: [{chamber, id, name, short_name,
//                                    description, sphere, url, start_url,
//                                    icons: [{src, sizes, type}]}]}
//
// A row is a plain link to the app's own page, so it opens in place — in a
// browser tab, an installed dashboard and the Android shell alike — and the
// app installs from there as a PWA of its own. The list is kept on the device
// (localStorage) so the page shows the last known apps offline; the apps
// themselves work offline through their own service workers.

import { esc } from './base.js';

const SRC = '/apps/index.json';
const CACHE_KEY = 'retinue_apps_index';

const CSS = `
  :host { display: block; }
  h2 { font-size: .82rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
       color: var(--muted, #8b93a3); margin: 14px 2px 10px; }
  .muted { color: var(--muted, #8b93a3); margin: 6px 2px; font-size: .9rem; }
  ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px;
       grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); }
  a { display: flex; gap: 12px; align-items: center; text-decoration: none; color: var(--fg, #e7ebf2);
      background: var(--card-2, #1c2230); border-radius: 12px; padding: 10px 12px;
      -webkit-tap-highlight-color: transparent; }
  a:hover, a:focus-visible { outline: 2px solid var(--accent, #6ea8fe); outline-offset: -2px; }
  img, .glyph { width: 44px; height: 44px; border-radius: 10px; flex: none; background: var(--card, #151922); }
  .glyph { display: inline-flex; align-items: center; justify-content: center; font-weight: 650;
           color: var(--accent, #6ea8fe); font-size: 1.2rem; }
  .name { font-weight: 600; }
  .desc { color: var(--muted, #8b93a3); font-size: .86rem; margin-top: 2px; }
  .where { color: var(--muted, #8b93a3); font-size: .74rem; margin-top: 2px; }
`;

function pickIcon(icons) {
  const list = Array.isArray(icons) ? icons : [];
  return (list.find((i) => /svg/.test(i.type || '') || i.sizes === 'any') || list[list.length - 1] || {}).src || '';
}

class RetinueApps extends HTMLElement {
  connectedCallback() {
    this.attachShadow({ mode: 'open' });
    let cached = null;
    try { cached = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null'); } catch (_) { cached = null; }
    this._render(cached, cached ? '' : 'Loading…');
    this.load();
  }

  async load() {
    try {
      const res = await fetch(SRC, { credentials: 'same-origin', cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      try { localStorage.setItem(CACHE_KEY, JSON.stringify(data)); } catch (_) { /* storage full */ }
      this._render(data, '');
    } catch (_err) {
      let cached = null;
      try { cached = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null'); } catch (_) { cached = null; }
      this._render(cached, cached ? 'Offline — the last known list.' : 'The list of apps could not be loaded.');
    }
  }

  _render(data, note) {
    const apps = (data && Array.isArray(data.apps)) ? data.apps : [];
    const rows = apps.map((a) => {
      const icon = pickIcon(a.icons);
      const glyph = icon
        ? `<img src="${esc(icon)}" alt="">`
        : `<span class="glyph" aria-hidden="true">${esc((a.short_name || a.name || '?').slice(0, 1).toUpperCase())}</span>`;
      return `<li><a href="${esc(a.start_url || a.url)}">${glyph}<span>` +
        `<div class="name">${esc(a.name)}</div>` +
        (a.description ? `<div class="desc">${esc(a.description)}</div>` : '') +
        `<div class="where">${esc(a.chamber)}${a.sphere ? ` · ${esc(a.sphere)}` : ''}</div>` +
        `</span></a></li>`;
    }).join('');
    const empty = data && !apps.length
      ? '<p class="muted">No chamber ships an app. A chamber adds one in <code>.retinue/apps/&lt;app&gt;/</code>.</p>' : '';
    this.shadowRoot.innerHTML = `<style>${CSS}</style><h2>Apps</h2>` +
      (note ? `<p class="muted">${esc(note)}</p>` : '') + empty + (rows ? `<ul>${rows}</ul>` : '');
  }
}

customElements.define('retinue-apps', RetinueApps);
