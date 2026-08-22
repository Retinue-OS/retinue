// Shell update handling for the dashboard PWA.
//
// The service worker already versions itself automatically (the gateway stamps
// its cache name with a content hash of the shell tree) and takes over
// immediately (skipWaiting + clients.claim). What it cannot do is put new code
// into a page that is already running — and the two places that bites are:
//
//  1. Every launch is one version behind: the browser's update check runs
//     alongside a navigation, so the launch that discovers a new version was
//     itself served the old one.
//  2. A window left open (the wide dashboard on a desktop) navigates never;
//     the browser re-checks sw.js on its own only about once a day, and even
//     then the running page keeps its old code indefinitely.
//
// This module closes both, on every page that imports it:
//
//  - controllerchange → reload once, so a newly activated worker's version is
//    on screen seconds after it lands — but only when that costs nothing:
//    never while the user is in a text field, and never while the
//    conversations card is in a thread or the composer (in-memory drafts and
//    recordings live there). A blocked reload stays pending and applies the
//    moment the coast is clear (checked again whenever visibility flips).
//  - visibility → visible triggers registration.update(), so the always-open
//    window checks for a new worker every time the user comes back to it
//    instead of once a day.
//
// It also defines <retinue-update-check> for the settings page: the running
// shell version plus a "Check for updates" action — the manual fallback, and
// the support answer to "which version are you on?".

const hasSW = 'serviceWorker' in navigator;

// A controllerchange also fires when the very first worker claims a fresh
// page; reloading then would flash the first visit for nothing. Only a page
// that was already controlled at load has actually been updated.
const hadController = hasSW && !!navigator.serviceWorker.controller;
let pendingReload = false;
let reloaded = false;

function textEntryActive() {
  // Follow the focus chain through shadow roots (all cards use them).
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  return !!el && (el.tagName === 'TEXTAREA'
    || (el.tagName === 'INPUT' && el.getAttribute('type') !== 'checkbox'));
}

function safeToReload() {
  // Thread and composer views hold uploads-in-progress and possibly a live
  // recording — never reload out from under them. Even back on the list view,
  // the card may hold typed-but-unsent drafts (its `dirty` getter says so).
  const conv = document.querySelector('retinue-conversations');
  if (conv) {
    const view = conv.getAttribute('data-view');
    if (view && view !== 'list') return false;
    if (conv.dirty) return false;
  }
  return !textEntryActive();
}

function tryReload() {
  if (!pendingReload || reloaded) return;
  if (!safeToReload()) return;
  reloaded = true;
  location.reload();
}

async function updateCheck() {
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    if (reg) await reg.update();
    return reg;
  } catch (_e) {
    return null; // offline — the next visibility flip tries again
  }
}

if (hasSW) {
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!hadController) return;
    pendingReload = true;
    tryReload();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') updateCheck();
    // Either flip is a chance to apply a reload that was blocked earlier —
    // going hidden is in fact the best moment (nobody sees it happen).
    tryReload();
  });
}

// Ask the *controlling* worker its shell hash — the version actually serving
// this page, which after an update differs from what the page loaded as.
function currentVersion() {
  return new Promise((resolve) => {
    if (!hasSW || !navigator.serviceWorker.controller) { resolve(null); return; }
    const ch = new MessageChannel();
    const timer = setTimeout(() => resolve(null), 2000);
    ch.port1.onmessage = (e) => {
      clearTimeout(timer);
      resolve(e.data && e.data.version ? String(e.data.version) : null);
    };
    navigator.serviceWorker.controller.postMessage({ type: 'get-version' }, [ch.port2]);
  });
}

// ── Settings-page element ─────────────────────────────────────────────────────

const CSS = `
  :host { display: block; }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
         color: var(--muted, #8b93a3); font-size: .85rem; }
  code { background: var(--card-2, #1c2230); border-radius: 6px; padding: 2px 7px;
         font-size: .8rem; color: var(--fg, #e7ebf2); }
  button { font: inherit; font-size: .8rem; cursor: pointer;
           background: transparent; color: var(--fg2, #c3cad6);
           border: 1px solid var(--line, rgba(231,235,242,.2)); border-radius: 8px;
           padding: 5px 10px; }
  button:hover { color: var(--fg, #e7ebf2); border-color: var(--accent, #6ea8fe); }
  button[disabled] { opacity: .6; cursor: default; }
  .status { font-size: .8rem; }
`;

class RetinueUpdateCheck extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `<style>${CSS}</style>` +
      `<div class="row"><span>App version</span><code>&#8230;</code>` +
      `<button type="button">Check for updates</button>` +
      `<span class="status"></span></div>`;
    this._code = this.shadowRoot.querySelector('code');
    this._btn = this.shadowRoot.querySelector('button');
    this._status = this.shadowRoot.querySelector('.status');
    this._btn.addEventListener('click', () => this._check());
    this._showVersion();
    if (!hasSW) {
      this._btn.disabled = true;
      this._status.textContent = 'Not available in this browser.';
    }
  }

  async _showVersion() {
    const v = await currentVersion();
    this._code.textContent = v || 'n/a';
  }

  async _check() {
    this._btn.disabled = true;
    this._status.textContent = 'Checking…';
    const reg = await updateCheck();
    if (!reg) {
      this._status.textContent = 'Check failed — offline?';
      this._btn.disabled = false;
      return;
    }
    if (reg.installing || reg.waiting) {
      // The new worker will activate (skipWaiting) and the controllerchange
      // handler above reloads this page — the settings page has no drafts to
      // guard, so that happens right away.
      this._status.textContent = 'Update found — installing…';
      pendingReload = true;
      setTimeout(() => tryReload(), 4000); // belt and braces if the event was missed
      return;
    }
    this._status.textContent = 'Up to date.';
    this._btn.disabled = false;
    this._showVersion();
  }
}
customElements.define('retinue-update-check', RetinueUpdateCheck);
