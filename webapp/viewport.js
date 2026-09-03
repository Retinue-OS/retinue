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

const root = document.documentElement;
const vv = window.visualViewport;

function apply() {
  if (!vv) return;
  if (vv.scale !== 1) {
    root.style.removeProperty('--frame-h');
    return;
  }
  root.style.setProperty('--frame-h', `${Math.round(vv.height)}px`);
}

if (vv) {
  vv.addEventListener('resize', apply);
  // Scroll fires when the visual viewport moves inside the layout viewport,
  // which is also when a bar has just hidden or shown.
  vv.addEventListener('scroll', apply);
  window.addEventListener('orientationchange', apply);
  apply();
}
