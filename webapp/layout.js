// Splitter manager for the dashboard's wide layout, VS Code style.
//
// Two draggable boundaries, declared in index.html as .splitter elements:
//   data-splitter="side"  — vertical bar; sets --side-w (projects column width)
//   data-splitter="news"  — horizontal bar; sets --news-h (news region height)
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
//   drag news below a threshold: snaps closed (display:none via data-news);
//                the splitter remains as the handle to pull it back open
//
// On phones the splitters are display:none (styles.css) and pointer math would
// be meaningless — every handler checks the wide-frame media query and no-ops
// otherwise. The module is dashboard-only (loaded by index.html alone).

import { WIDE_FRAME } from './components/base.js';

const STORE_KEY = 'retinue.layout.v1';
const SNAP_CLOSED_PX = 60;   // dragging news shorter than this closes it
const KEY_STEP_PX = 32;      // arrow-key resize increment
const MIN_SIDE_PX = 280;     // keep in sync with .col-side min-width
const MAX_SIDE_FRACTION = 0.45;   // …and max-width
const MAX_NEWS_FRACTION = 0.75;   // …and retinue-news max-height

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
// stylesheet defaults). data-news="closed" is how a zero-height news region is
// expressed — display:none rather than a squashed 0px scroll box.
function apply() {
  if (typeof sizes.side === 'number') {
    mainEl.style.setProperty('--side-w', `${sizes.side}px`);
  } else {
    mainEl.style.removeProperty('--side-w');
  }
  if (sizes.news === 'closed') {
    colMain.setAttribute('data-news', 'closed');
    mainEl.style.removeProperty('--news-h');
  } else {
    colMain.removeAttribute('data-news');
    if (typeof sizes.news === 'number') {
      mainEl.style.setProperty('--news-h', `${sizes.news}px`);
    } else {
      mainEl.style.removeProperty('--news-h');
    }
  }
}

// Current pixel size of a region, measured (not read from the vars, which may
// hold the stylesheet's percentage default).
function currentSize(which) {
  if (which === 'side') {
    const col = document.querySelector('.col-side');
    return col ? col.getBoundingClientRect().width : 0;
  }
  const news = document.querySelector('retinue-news');
  return news && sizes.news !== 'closed' ? news.getBoundingClientRect().height : 0;
}

function clampSize(which, px) {
  if (which === 'side') {
    const max = deckEl.getBoundingClientRect().width * MAX_SIDE_FRACTION;
    return Math.min(Math.max(px, MIN_SIDE_PX), max);
  }
  // News may go all the way to 0 — small values snap to closed in setSize.
  const max = colMain.getBoundingClientRect().height * MAX_NEWS_FRACTION;
  return Math.min(Math.max(px, 0), max);
}

function setSize(which, px) {
  if (which === 'news' && px < SNAP_CLOSED_PX) {
    sizes.news = 'closed';
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
      // Both regions sit on the far side of their splitter (projects to the
      // right, news below), so growing means dragging toward the start edge.
      const delta = startPos - (vertical ? ev.clientX : ev.clientY);
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
    const grow = vertical ? ['ArrowLeft'] : ['ArrowUp'];
    const shrink = vertical ? ['ArrowRight'] : ['ArrowDown'];
    let delta = 0;
    if (grow.includes(e.key)) delta = KEY_STEP_PX;
    else if (shrink.includes(e.key)) delta = -KEY_STEP_PX;
    else if (e.key === 'Enter') { resetSize(which); return; }
    else return;
    e.preventDefault();
    // Reopening a closed news region by keyboard starts from a usable height.
    const base = (which === 'news' && sizes.news === 'closed' && delta > 0)
      ? SNAP_CLOSED_PX : currentSize(which);
    setSize(which, base + delta);
    saveSizes(sizes);
  });
}

if (mainEl && deckEl && colMain) {
  apply();
  document.querySelectorAll('.splitter[data-splitter]').forEach(wireSplitter);
}
