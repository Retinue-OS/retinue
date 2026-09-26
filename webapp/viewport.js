// Visible-height tracker for the frame-locked pages.
//
// The chat page, the project page and an open conversation thread lock <main>
// to the viewport (styles.css, chat.html) so the composer stays pinned at the
// bottom while the thread scrolls internally. The lock is `height: 100dvh`,
// and on an Android phone that unit can be taller than what is actually on
// screen: with Chrome's edge-to-edge mode the gesture-navigation "chin" is a
// dynamic bottom bar that hides on scroll, and the frame is laid out for the
// bar-hidden height while the bar is still showing. The bottom row — the
// message field — then sits below the visible edge, and because the thread
// swallows every drag (overscroll-behavior: contain, on purpose), only a drag
// that starts on the header or on the chips above the field can scroll the
// page enough to bring it into view. The on-screen keyboard can produce the
// same mismatch where the browser shrinks only the visual viewport.
//
// The visual viewport is the one measurement that is what the user sees, so
// the locked frames size themselves from it: this module keeps --frame-h on
// <html> equal to window.visualViewport.height, and the frame rules read
// `height: var(--frame-h, 100dvh)`. Where the API is missing the stylesheet's
// dvh fallback stands as before. While pinch-zoomed the visual viewport is a
// magnified crop and its height means nothing for layout, so the property is
// dropped until the scale is back to 1.
//
// The same measurement answers a second question for pages that change their
// layout while the on-screen keyboard is up (the chat page's typing mode):
// is the visible frame well short of the tallest it has been at this width?
// That is published as data-viewport-short="1" on <html>, with a
// `retinue-viewport` event on window whenever it flips, so there is one
// interpretation of the viewport rather than one per page. The baseline is
// kept per width, so a rotation with the keyboard up does not learn the
// keyboard-shrunk height as the keyboard-less one; and while pinch-zoomed
// the flag is dropped, as --frame-h is. A short frame alone does not mean
// "keyboard": a page combines it with whether one of its fields has focus.

const root = document.documentElement;
const vv = window.visualViewport;

// Tallest visible height seen per viewport width (the keyboard-less frame).
const tallest = new Map();
// How much shorter than that counts as short: a keyboard takes 35–50% of a
// phone's frame, a browser bar showing or hiding well under 20%.
const SHORT = 0.8;

function setShort(short) {
  if ((root.dataset.viewportShort === '1') === short) return;
  if (short) root.dataset.viewportShort = '1';
  else delete root.dataset.viewportShort;
  window.dispatchEvent(new CustomEvent('retinue-viewport', { detail: { short } }));
}

function apply() {
  if (!vv) return;
  if (vv.scale !== 1) {
    root.style.removeProperty('--frame-h');
    setShort(false);
    return;
  }
  root.style.setProperty('--frame-h', `${Math.round(vv.height)}px`);
  const w = Math.round(vv.width);
  const max = Math.max(tallest.get(w) || 0, vv.height);
  tallest.set(w, max);
  setShort(vv.height < max * SHORT);
}

if (vv) {
  vv.addEventListener('resize', apply);
  // Scroll fires when the visual viewport moves inside the layout viewport,
  // which is also when a bar has just hidden or shown.
  vv.addEventListener('scroll', apply);
  window.addEventListener('orientationchange', apply);
  apply();
}
