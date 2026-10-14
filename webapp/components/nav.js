// The dashboard's navigation: one row at the top of every page — the home
// (the attention list), the pages beside it, the pending sends and settings,
// and the drill-downs (a chat, a project, an open thread) as well.
//
// The way home used to be a "← Back to dashboard" link at the end of each list
// page, so a long list had to be scrolled to its tail to leave it, and the
// home's own links to those pages sat at the foot of the attention list. An
// installed PWA has no browser chrome to fall back on, so this row is the way
// out of a page, not a convenience: it is the first thing on the page and
// stays pinned while a list page scrolls (the sticky placement is styles.css's,
// which owns page layout).
//
// The drill-downs used to be immersive, with no row and only a back button
// in their own bar, so a chat opened from a push was a dead end: back to the
// home, then across. They carry the row now, and their back button moved into
// it: `back` puts a ‹ at the row's start. The row does not decide where back
// goes — that differs per page (a chat opened cold goes home, a thread on the
// home closes in place) — it dispatches `retinue-back` (bubbling, composed) on
// itself, and the page listening on this element does the going. A modified
// click follows the button's href, the home, like any link.
//
//   <retinue-nav current="chats"></retinue-nav>
//   <retinue-nav back current="chats"></retinue-nav>   (a drill-down)
//
// `current` names the page (home · chats · threads · projects · news ·
// contacts · sends · settings); without it the page is recognised by its
// path. `back` may come and go (the thread viewer on the home sets it while a
// thread is open). Plain links, so the row works offline from the service
// worker's shell cache (the sends page is the gateway's own, so it needs the
// network).
//
// Contacts and the sends queue used to sit in an app dock at the foot of the
// home, beside the phone's own dialer, SMS, mail and map buttons. Those four
// are the Android shell's launcher bar now (they are the OS's, not Retinue's),
// and the two that are Retinue's moved here: the address book is a place you
// browse, like the chats, so it is a tab; the sends waiting for approval are
// a queue that wants action, so they are an icon one tap away, beside the gear.

import { esc } from './base.js';
// Every page carries this row, so it brings the Android shell's
// fallback with it: a wake or Home press on a page that is not the home
// goes there (components/shell.js). Its links ask the same question a Home
// press does: on a page holding unsent input (a typed message, an unsaved
// project edit, a dictation on its way) a tap on a tab stays put and says
// why, instead of dropping the input — the drill-downs that carry the row
// now are where such input lives.
import { shellEventHeld } from './shell.js';

const PAGES = [
  { id: 'home', href: '/', label: 'Home', paths: ['/', '/index.html'] },
  { id: 'chats', href: '/chats.html', label: 'Chats', paths: ['/chats.html', '/chat.html'] },
  { id: 'threads', href: '/conversations.html', label: 'Threads', paths: ['/conversations.html'] },
  { id: 'projects', href: '/projects.html', label: 'Projects', paths: ['/projects.html', '/project.html'] },
  { id: 'news', href: '/news.html', label: 'News', paths: ['/news.html'] },
  { id: 'contacts', href: '/contacts.html', label: 'Contacts', paths: ['/contacts.html'] },
];
// The icons at the right end: the outbox, then the gear.
const SENDS = { id: 'sends', href: '/sends', label: 'Pending sends', paths: ['/sends'] };
const SETTINGS = { id: 'settings', href: '/settings.html', label: 'Settings', paths: ['/settings.html'] };
// An outbox tray with its arrow leaving it — drawn rather than an emoji, so
// it takes the row's colour like the gear beside it.
const OUTBOX_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M12 14V3"/><path d="M8 7l4-4 4 4"/>' +
  '<path d="M3 13h5l1.5 3h5L16 13h5v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

const CSS = `
  :host { display: block; }
  * { box-sizing: border-box; }
  nav { display: flex; align-items: stretch; gap: 4px;
        border-bottom: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  /* Text tabs with an accent rule under the page you are on: they read as
     places, not as a filter — the segmented Active/Archived controls on the
     list pages below are filters, and two filled bars stacked would blur the
     difference. A phone narrower than the six labels scrolls the row
     sideways rather than truncating a word. */
  .tabs { flex: 1; min-width: 0; display: flex; gap: 2px; overflow-x: auto;
          scrollbar-width: none; -webkit-overflow-scrolling: touch; }
  .tabs::-webkit-scrollbar { display: none; }
  a { color: var(--muted, #8b93a3); text-decoration: none; white-space: nowrap;
      -webkit-tap-highlight-color: transparent; }
  a:focus-visible { outline: 2px solid var(--accent, #6ea8fe); outline-offset: -2px; border-radius: 8px; }
  .tab { flex: 1 0 auto; text-align: center; padding: 8px 5px 9px; font-size: .86rem;
         font-weight: 500; border-bottom: 2px solid transparent; margin-bottom: -1px; }
  .tab:hover { color: var(--fg, #e7ebf2); }
  .tab[aria-current="page"] { color: var(--fg, #e7ebf2); font-weight: 650;
                              border-bottom-color: var(--accent, #6ea8fe); }
  /* The icon links at the right end — the pending sends and the gear. */
  .icon { flex: none; display: inline-flex; align-items: center; justify-content: center;
          width: 32px; font-size: 1.05rem; border-bottom: 2px solid transparent; margin-bottom: -1px; }
  .icon:hover { color: var(--accent, #6ea8fe); }
  .icon[aria-current="page"] { color: var(--fg, #e7ebf2); border-bottom-color: var(--accent, #6ea8fe); }
  /* The drill-downs' back button, at the row's start — the same round ‹ their
     own bars carried before it moved here. */
  .back { flex: none; align-self: center; width: 34px; height: 34px; margin: 0 6px 2px 0;
          border-radius: 50%; background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2);
          font-size: 1.35rem; line-height: 1; display: inline-flex; align-items: center;
          justify-content: center; padding: 0 2px 2px 0; }
  .back:hover { color: var(--accent, #6ea8fe); }
  /* Wide screens have the room: the places sit together on the left instead
     of spreading across a 1600px frame. */
  @media (min-width: 700px) { .tab { flex: 0 0 auto; padding: 8px 14px 9px; } }
`;

function currentPage(attr) {
  if (attr) return attr;
  const path = location.pathname;
  const hit = [...PAGES, SENDS, SETTINGS].find((p) => p.paths.includes(path));
  return hit ? hit.id : '';
}

class RetinueNav extends HTMLElement {
  static get observedAttributes() { return ['back', 'current']; }

  connectedCallback() { this._render(); }

  attributeChangedCallback(name, before, after) {
    if (before !== after && this.shadowRoot) this._render();
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    const cur = currentPage(this.getAttribute('current'));
    const link = (p, cls, inner) =>
      `<a class="${cls}" href="${p.href}"${p.id === cur ? ' aria-current="page"' : ''}` +
      `${cls === 'icon' ? ` title="${p.label}" aria-label="${p.label}"` : ''}>${inner}</a>`;
    const back = this.hasAttribute('back')
      ? '<a class="back" href="/" data-back title="Back" aria-label="Back">&#8249;</a>' : '';
    this.shadowRoot.innerHTML = `<style>${CSS}</style>` +
      `<nav aria-label="Dashboard">${back}<div class="tabs">` +
      PAGES.map((p) => link(p, 'tab', esc(p.label))).join('') +
      `</div>${link(SENDS, 'icon', OUTBOX_SVG)}${link(SETTINGS, 'icon', '&#9881;')}</nav>`;
    const btn = this.shadowRoot.querySelector('[data-back]');
    if (btn) btn.addEventListener('click', (e) => this._onBack(e));
    const nav = this.shadowRoot.querySelector('nav');
    if (nav) nav.addEventListener('click', (e) => this._onLink(e));
  }

  // A tab or icon press on a page holding unsent input is held, with the
  // shell's note, like a Home press (a modified click opens elsewhere and
  // loses nothing). The back button is the page's to decide.
  _onLink(e) {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
    const a = e.target && e.target.closest ? e.target.closest('a') : null;
    if (!a || a.hasAttribute('data-back')) return;
    if (shellEventHeld('home')) e.preventDefault();
  }

  // A plain press hands back to the page; a modified one (new tab, new
  // window) follows the link to the home, as any link would.
  _onBack(e) {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
    e.preventDefault();
    this.dispatchEvent(new CustomEvent('retinue-back', { bubbles: true, composed: true }));
  }
}

customElements.define('retinue-nav', RetinueNav);
