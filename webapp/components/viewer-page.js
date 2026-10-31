// The file viewer (/viewer.html): one thread attachment shown inside the
// dashboard, under the navigation row and a bar with its name and a save link.
//
// A file row's view link used to navigate straight to the attachment URL
// (`?inline=1`), so the browser's own PDF or image viewer took the whole
// window. In a tab that is fine — the browser's back button is there — but an
// installed PWA has no browser chrome, and the bare file carries none of the
// dashboard's: the only way back was the platform's back gesture, where there
// is one. Here the file sits in a frame of the dashboard's own page, so the
// row's ‹ (and every tab beside it) is always one tap away.
//
//   /viewer.html?src=<attachment path>&name=<file name>&type=<media type>
//
// `src` must be a thread attachment path of this dashboard
// (/conversations/<id>/attachments/<att-id>[/<name>]) — anything else is
// refused, so the page cannot be pointed at another origin or a script URL.
// `name` is shown as text only; `type` picks how the file is drawn and is
// checked against the types the gateway serves inline.
//
// A browser without a built-in PDF viewer (navigator.pdfViewerEnabled false —
// Chrome on Android) would draw an empty frame, so it gets the Open and Save
// links instead; Open hands the file to the browser as before. The Android
// shell never comes here: its WebView renders no PDF and its download
// listener opens the phone's viewer app, so its rows keep the direct link
// (conversation.js).

import { esc } from './base.js';

const SRC_RE = /^\/conversations\/[0-9a-f]{32}\/attachments\/[0-9a-f]{32}(\/[^?#]*)?$/;
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif']);
const HOME_URL = '/';

const CSS = `
  :host { display: flex; flex-direction: column; min-height: 0; gap: 8px; }
  * { box-sizing: border-box; }
  .bar { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .name { flex: 1; min-width: 0; margin: 0; font-size: .95rem; font-weight: 600;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--fg, #e7ebf2); }
  a.btn { flex: none; display: inline-flex; align-items: center; gap: 4px; padding: 6px 12px;
          border-radius: 999px; background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2);
          text-decoration: none; font-size: .86rem; -webkit-tap-highlight-color: transparent; }
  a.btn:hover { color: var(--accent, #6ea8fe); }
  a.btn:focus-visible { outline: 2px solid var(--accent, #6ea8fe); outline-offset: 2px; }
  .stage { flex: 1; min-height: 0; display: flex; border-radius: 12px; overflow: hidden;
           background: var(--card, #141925); border: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  iframe { flex: 1; width: 100%; height: 100%; border: 0; background: #fff; }
  .img { flex: 1; min-height: 0; overflow: auto; display: flex; align-items: center; justify-content: center; }
  .img img { max-width: 100%; max-height: 100%; object-fit: contain; }
  .note { margin: auto; padding: 24px; text-align: center; color: var(--muted, #8b93a3); line-height: 1.5; }
  .note .acts { display: flex; gap: 8px; justify-content: center; margin-top: 12px; }
`;

// What the query names, validated; null when the page cannot show it.
export function parseViewerParams(search) {
  const q = new URLSearchParams(search || '');
  const src = q.get('src') || '';
  if (!SRC_RE.test(src)) return null;
  const type = (q.get('type') || '').split(';')[0].trim().toLowerCase();
  const kind = type === 'application/pdf' ? 'pdf'
    : IMAGE_TYPES.has(type) ? 'image'
      : type === 'text/plain' ? 'text' : '';
  if (!kind) return null;
  // Without a name, the URL's own last segment (percent-encoded) stands in.
  const name = q.get('name') || safeDecode(src.split('/')[5] || '') || 'attachment';
  return { src, name, kind };
}

function safeDecode(s) {
  try { return decodeURIComponent(s); } catch (_) { return s; }
}

class RetinueViewerPage extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._nav = document.querySelector('retinue-nav');
    this._onBack = () => this._goBack();
    if (this._nav) this._nav.addEventListener('retinue-back', this._onBack);
    this._render();
  }

  disconnectedCallback() {
    if (this._nav) this._nav.removeEventListener('retinue-back', this._onBack);
    this._nav = null;
  }

  // Back to where the file was opened from — the thread, with its scroll and
  // open state, from the history entry (or the back/forward cache). A viewer
  // opened cold (a pasted link, a fresh window) has nothing of the
  // dashboard's behind it, so it goes home instead of leaving the app.
  _goBack() {
    let fromApp = false;
    try { fromApp = !!document.referrer && new URL(document.referrer).origin === location.origin; } catch (_) { /* no referrer */ }
    if (fromApp && history.length > 1) { history.back(); return; }
    location.href = HOME_URL;
  }

  _render() {
    const p = parseViewerParams(location.search);
    if (!p) {
      this.shadowRoot.innerHTML = `<style>${CSS}</style>` +
        `<div class="stage"><p class="note">This file cannot be shown here.</p></div>`;
      return;
    }
    document.title = `Retinue — ${p.name}`;
    const view = `${p.src}?inline=1`;
    const save = `<a class="btn" href="${esc(p.src)}" download="${esc(p.name)}" title="Save a copy">↓ Save</a>`;
    const open = `<a class="btn" href="${esc(view)}">Open</a>`;
    let stage;
    if (p.kind === 'image') {
      stage = `<div class="img"><img src="${esc(view)}" alt="${esc(p.name)}"></div>`;
    } else if (p.kind === 'pdf' && navigator.pdfViewerEnabled === false) {
      stage = `<div class="note">This browser cannot show a PDF inside the page.` +
        `<div class="acts">${open}${save}</div></div>`;
    } else {
      // No sandbox on a PDF: a sandboxed frame keeps the browser's PDF viewer
      // out. The file is the thread's own, served nosniff with its stored type.
      const sandbox = p.kind === 'text' ? ' sandbox=""' : '';
      stage = `<iframe src="${esc(view)}" title="${esc(p.name)}"${sandbox}></iframe>`;
    }
    this.shadowRoot.innerHTML = `<style>${CSS}</style>` +
      `<div class="bar"><h1 class="name" title="${esc(p.name)}">${esc(p.name)}</h1>${save}</div>` +
      `<div class="stage">${stage}</div>`;
  }
}

if (!customElements.get('retinue-viewer-page')) {
  customElements.define('retinue-viewer-page', RetinueViewerPage);
}
