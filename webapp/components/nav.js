// The dashboard's navigation: one row at the top of every top-level page —
// the home (the attention list) and the pages beside it, plus the pending
// sends and settings.
//
// The way home used to be a "← Back to dashboard" link at the end of each list
// page, so a long list had to be scrolled to its tail to leave it, and the
// home's own links to those pages sat at the foot of the attention list. An
// installed PWA has no browser chrome to fall back on, so this row is the way
// out of a page, not a convenience: it is the first thing on the page and
// stays pinned while a list page scrolls (the sticky placement is styles.css's,
// which owns page layout). The drill-downs — a chat, a project, an open thread
// — keep their own back button in their bar instead; they are immersive, and
// back is the one move they need.
//
//   <retinue-nav current="chats"></retinue-nav>
//
// `current` names the page (home · chats · threads · projects · news ·
// contacts · sends · settings); without it the page is recognised by its
// path. Plain links, so the row works offline from the service worker's shell
// cache (the sends page is the gateway's own, so it needs the network).
//
// Contacts and the sends queue used to sit in an app dock at the foot of the
// home, beside the phone's own dialer, SMS, mail and map buttons. Those four
// are the Android shell's launcher bar now (they are the OS's, not Retinue's),
// and the two that are Retinue's moved here: the address book is a place you
// browse, like the chats, so it is a tab; the sends waiting for approval are
// a queue that wants action, so they are an icon one tap away, beside the gear.

import { esc } from './base.js';
// Every top-level page carries this row, so it brings the Android shell's
// fallback with it: a wake or Home press on a page that is not the home
// goes there (components/shell.js).
import './shell.js';

const PAGES = [
  { id: 'home', href: '/', label: 'Home', paths: ['/', '/index.html'] },
  { id: 'chats', href: '/chats.html', label: 'Chats', paths: ['/chats.html'] },
  { id: 'threads', href: '/conversations.html', label: 'Threads', paths: ['/conversations.html'] },
  { id: 'projects', href: '/projects.html', label: 'Projects', paths: ['/projects.html'] },
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
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    const cur = currentPage(this.getAttribute('current'));
    const link = (p, cls, inner) =>
      `<a class="${cls}" href="${p.href}"${p.id === cur ? ' aria-current="page"' : ''}` +
      `${cls === 'icon' ? ` title="${p.label}" aria-label="${p.label}"` : ''}>${inner}</a>`;
    this.shadowRoot.innerHTML = `<style>${CSS}</style>` +
      `<nav aria-label="Dashboard"><div class="tabs">` +
      PAGES.map((p) => link(p, 'tab', esc(p.label))).join('') +
      `</div>${link(SENDS, 'icon', OUTBOX_SVG)}${link(SETTINGS, 'icon', '&#9881;')}</nav>`;
  }
}

customElements.define('retinue-nav', RetinueNav);
