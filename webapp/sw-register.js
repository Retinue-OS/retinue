// Registers the service worker (sw.js), so the page, and the shell it
// belongs to, open with no connectivity too.
//
// A classic script at the end of every page's body, where the inline script
// it replaces stood: the pages' Content-Security-Policy runs scripts only from
// the dashboard's own files, never from markup (docs/dashboard.md, "The
// Content-Security-Policy").
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js'));
}
