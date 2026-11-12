// The instance settings page on its own (settings-instance.html), as the
// Android app embeds it. A module of its own where it used to be an inline
// module at the end of the page, in the same place in the load order: the
// pages' Content-Security-Policy runs no inline script (docs/dashboard.md,
// "The Content-Security-Policy").

// Embedded by the app's settings page, which is served by the app itself
// at this reserved address: tell it the section is here and how tall it
// is, so the frame needs no scrolling of its own. The message names that
// origin, so a page that is not the app's never receives it; opened on
// its own, this is a plain page.
//
// The section is the last part of a long page, so its frame starts out of
// sight, and Chromium does not render a frame of another origin while it
// is out of sight: no ResizeObserver, no animation frames. So the height
// also goes out when the section's content changes, measured at once (a
// timer, not a frame); the observer covers the rest once it is in view.
const APP_ORIGIN = 'https://appassets.androidplatform.net';
if (window.parent !== window) {
  let last = -1;
  const post = () => {
    const height = Math.ceil(document.querySelector('main').getBoundingClientRect().height);
    if (height === last) return;
    last = height;
    window.parent.postMessage({ type: 'retinue-instance-settings', state: 'ready', height }, APP_ORIGIN);
  };
  new ResizeObserver(post).observe(document.querySelector('main'));
  document.addEventListener('retinue-instance-settings-change', () => setTimeout(post, 0));
  // Fonts and styles that arrive late change the height too.
  window.addEventListener('load', () => setTimeout(post, 0));
  post();
}
