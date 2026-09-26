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
// The same measurement, with focus, answers a second question for pages that
// change their layout while the on-screen keyboard is up (the chat page's
// typing mode): is the keyboard up? That is published as data-keyboard="1"
// on <html>, with a `retinue-viewport` event on window whenever the flag or
// the visible height changes while it is set (the keyboard animates in steps
// on iOS), so there is one interpretation of the viewport rather than one per
// page. The rule:
//  - With no text field focused there is no keyboard, and the visible height
//    IS the keyboard-less height for this width. It is re-learned on every
//    reading, so a split-screen or a browser bar can never leave behind a
//    stale "tallest" that makes an ordinary frame look short.
//  - With a field focused, the keyboard is up when the frame is well short of
//    that width's keyboard-less height.
//  - A width seen for the first time while a field is focused (rotating with
//    the keyboard up) has no keyboard-less height yet. The keyboard state
//    carries over from the old width, and that width's height is learned when
//    a reading clearly taller than the shortest one comes (the keyboard went
//    down) or when the field loses focus.
//  - While pinch-zoomed the flag is dropped, as --frame-h is.

import { deepActiveElement, isTextEntry } from './components/base.js';

const root = document.documentElement;
const vv = window.visualViewport;

// How much shorter than keyboard-less counts as a keyboard: one takes 35–50%
// of a phone's frame, a browser bar showing or hiding well under 20%.
const SHORT = 0.8;
// Keyboard-less visible height per viewport width.
const baseline = new Map();
// A width being learned with the keyboard carried over: {width, lowest}.
let carry = null;
let keyboard = false;
let lastH = 0;

function publish(up, h) {
  const changed = up !== keyboard || (up && h !== lastH);
  keyboard = up;
  lastH = h;
  if (up) root.dataset.keyboard = '1';
  else delete root.dataset.keyboard;
  if (changed) window.dispatchEvent(new CustomEvent('retinue-viewport', { detail: { keyboard: up, height: h } }));
}

function apply() {
  if (!vv) return;
  if (vv.scale !== 1) {
    root.style.removeProperty('--frame-h');
    carry = null;
    publish(false, 0);
    return;
  }
  const h = Math.round(vv.height);
  const w = Math.round(vv.width);
  root.style.setProperty('--frame-h', `${h}px`);
  if (!isTextEntry(deepActiveElement())) {
    baseline.set(w, h);
    carry = null;
    publish(false, h);
    return;
  }
  if (carry && carry.width !== w) carry = null;
  const base = baseline.get(w);
  if (base === undefined || carry) {
    if (!keyboard) { baseline.set(w, h); publish(false, h); return; }
    carry = carry || { width: w, lowest: h };
    carry.lowest = Math.min(carry.lowest, h);
    if (carry.lowest < h * SHORT) {
      // Clearly taller than the frame the keyboard left: it went down.
      baseline.set(w, h);
      carry = null;
      publish(false, h);
    } else {
      publish(true, h);
    }
    return;
  }
  const max = Math.max(base, h);
  baseline.set(w, max);
  publish(h < max * SHORT, h);
}

if (vv) {
  vv.addEventListener('resize', apply);
  // Scroll fires when the visual viewport moves inside the layout viewport,
  // which is also when a bar has just hidden or shown.
  vv.addEventListener('scroll', apply);
  window.addEventListener('orientationchange', apply);
  // Focus decides whether a reading is a baseline or a comparison. Focus
  // moving between two fields passes through a blur: settle after it lands.
  // (A move that stays inside one component's shadow tree reaches no
  // document listener; the keyboard's own resize re-reads focus then.)
  document.addEventListener('focusin', apply);
  document.addEventListener('focusout', () => setTimeout(apply, 0));
  apply();
}
