// Splitter manager for the dashboard's wide layout, VS Code style.
//
// Three draggable boundaries, declared in index.html as .splitter elements:
//   data-splitter="side"  — vertical bar; sets --side-w (projects column width)
//   data-splitter="news"  — horizontal bar; sets --news-h (news region height)
//   data-splitter="chats" — horizontal bar; sets --chats-h (chats region
//                           height). Ships commented out beside the chats card;
//                           everything here is a no-op while it is absent.
//
// Sizes live as CSS custom properties on <main>; styles.css supplies the
// defaults when a property is unset, so this module only ever *overrides* the
// stylesheet, never replaces it. Chosen sizes persist per device in
// localStorage and are re-applied on load. CSS min/max clamps on the columns
// keep a persisted size sane when the window shrinks.
//
// Interactions, matching editor conventions:
//   drag         resize (pointer events, so mouse/pen/touch all work)
//   double-click reset this boundary to the stylesheet default
//   arrow keys   resize in steps (the splitters are focusable separators)
//   drag news/chats below a threshold: snaps closed (display:none via a
//                data-* attribute); the splitter remains as the handle to
//                pull the region back open
//
// On phones the splitters are display:none (styles.css) and pointer math would
// be meaningless — every handler checks the wide-frame media query and no-ops
// otherwise. The module is dashboard-only (loaded by index.html alone).

import { WIDE_FRAME } from './components/base.js';

const STORE_KEY = 'retinue.layout.v1';
const SNAP_CLOSED_PX = 60;   // dragging news/chats shorter than this closes it
const KEY_STEP_PX = 32;      // arrow-key resize increment
const MIN_SIDE_PX = 280;     // keep in sync with .col-side min-width
const MAX_SIDE_FRACTION = 0.45;   // …and max-width
const MAX_NEWS_FRACTION = 0.75;   // …and retinue-news max-height
const MAX_CHATS_FRACTION = 0.6;   // leave the conversations below real room

const mainEl = document.querySelector('main');
const deckEl = document.querySelector('.deck');
const colMain = document.querySelector('.col-main');
const wide = matchMedia(WIDE_FRAME);

function loadSizes() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY)) || {}; }
  catch (_e) { return {}; }
}

function saveSizes(sizes) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(sizes)); }
  catch (_e) { /* private mode: resizing still works for the session */ }
}

let sizes = loadSizes();

// Push the stored sizes into the CSS properties (or clear them back to the
// stylesheet defaults). data-news/data-chats "closed" is how a zero-height
// region is expressed — display:none rather than a squashed 0px scroll box.
function applyClosable(which, attr, prop) {
  if (sizes[which] === 'closed') {
    colMain.setAttribute(attr, 'closed');
    mainEl.style.removeProperty(prop);
  } else {
    colMain.removeAttribute(attr);
    if (typeof sizes[which] === 'number') {
      mainEl.style.setProperty(prop, `${sizes[which]}px`);
    } else {
      mainEl.style.removeProperty(prop);
    }
  }
}

function apply() {
  if (typeof sizes.side === 'number') {
    mainEl.style.setProperty('--side-w', `${sizes.side}px`);
  } else {
    mainEl.style.removeProperty('--side-w');
  }
  applyClosable('news', 'data-news', '--news-h');
  applyClosable('chats', 'data-chats', '--chats-h');
}

// Current pixel size of a region, measured (not read from the vars, which may
// hold the stylesheet's percentage default). The chats card ships commented
// out — an absent element simply measures 0.
function currentSize(which) {
  if (which === 'side') {
    const col = document.querySelector('.col-side');
    return col ? col.getBoundingClientRect().width : 0;
  }
  const el = document.querySelector(which === 'chats' ? 'retinue-chats' : 'retinue-news');
  return el && sizes[which] !== 'closed' ? el.getBoundingClientRect().height : 0;
}

function clampSize(which, px) {
  if (which === 'side') {
    const max = deckEl.getBoundingClientRect().width * MAX_SIDE_FRACTION;
    return Math.min(Math.max(px, MIN_SIDE_PX), max);
  }
  // News and chats may go all the way to 0 — small values snap to closed in
  // setSize.
  const fraction = which === 'chats' ? MAX_CHATS_FRACTION : MAX_NEWS_FRACTION;
  const max = colMain.getBoundingClientRect().height * fraction;
  return Math.min(Math.max(px, 0), max);
}

function setSize(which, px) {
  if ((which === 'news' || which === 'chats') && px < SNAP_CLOSED_PX) {
    sizes[which] = 'closed';
  } else {
    sizes[which] = Math.round(clampSize(which, px));
  }
  apply();
}

function resetSize(which) {
  delete sizes[which];
  apply();
  saveSizes(sizes);
}

function wireSplitter(el) {
  const which = el.dataset.splitter;
  const vertical = which === 'side';

  el.addEventListener('pointerdown', (e) => {
    if (!wide.matches) return;
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add('dragging');
    const startPos = vertical ? e.clientX : e.clientY;
    const startSize = currentSize(which);

    const move = (ev) => {
      // Projects and news sit on the far side of their splitter (right /
      // below), so they grow when the drag moves toward the start edge; the
      // chats region sits above its splitter and grows the other way.
      const toward = startPos - (vertical ? ev.clientX : ev.clientY);
      const delta = which === 'chats' ? -toward : toward;
      setSize(which, startSize + delta);
    };
    const up = () => {
      el.classList.remove('dragging');
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      saveSizes(sizes);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  });

  el.addEventListener('dblclick', () => { if (wide.matches) resetSize(which); });

  el.addEventListener('keydown', (e) => {
    if (!wide.matches) return;
    // Growing follows the drag direction: toward the start edge for side and
    // news, away from it for chats (which sits above its splitter).
    const grow = vertical ? ['ArrowLeft'] : (which === 'chats' ? ['ArrowDown'] : ['ArrowUp']);
    const shrink = vertical ? ['ArrowRight'] : (which === 'chats' ? ['ArrowUp'] : ['ArrowDown']);
    let delta = 0;
    if (grow.includes(e.key)) delta = KEY_STEP_PX;
    else if (shrink.includes(e.key)) delta = -KEY_STEP_PX;
    else if (e.key === 'Enter') { resetSize(which); return; }
    else return;
    e.preventDefault();
    // Reopening a closed region by keyboard starts from a usable height.
    const base = ((which === 'news' || which === 'chats') && sizes[which] === 'closed' && delta > 0)
      ? SNAP_CLOSED_PX : currentSize(which);
    setSize(which, base + delta);
    saveSizes(sizes);
  });
}

if (mainEl && deckEl && colMain) {
  apply();
  document.querySelectorAll('.splitter[data-splitter]').forEach(wireSplitter);
}
