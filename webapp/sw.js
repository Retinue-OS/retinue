/* Retinue dashboard service worker.
 *
 * Strategy:
 *  - Shell assets (HTML/CSS/JS/icons): cache-first, so the dashboard -- and
 *    crucially the local app-launch buttons like the dialer -- open instantly
 *    and work with no connectivity.
 *  - Data documents (/data/*.json): network-first with cache fallback, so you
 *    see fresh curated content when online and the last known state offline.
 *
 * Note: the endpoint sits behind HTTP basic auth. The browser attaches the
 * cached credentials automatically to these same-origin GETs, so both install
 * and runtime fetches work once you have authenticated once.
 */
const SHELL = 'retinue-shell-v14';
const DATA = 'retinue-data-v1';
const SHELL_ASSETS = [
  '/',
  '/conversations.html',
  '/projects.html',
  '/project.html',
  '/styles.css',
  '/manifest.webmanifest',
  '/components/base.js',
  '/components/markdown.js',
  '/components/conversations.js',
  '/components/projects.js',
  '/components/project-page.js',
  '/components/app-launcher.js',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;

  // Conversation API is dynamic (live chat with Ara): never serve from cache,
  // just pass through to the network so threads and replies stay current.
  if (url.pathname === '/conversations' || url.pathname.startsWith('/conversations/')) return;

  // The projects endpoints are live (SPARQL over the life store, and the
  // editable per-project file at /projects/item); always go to the network so
  // views and the editor never work on a stale cached copy. The page shells
  // (/projects.html, /project.html) are separate paths and stay cache-first.
  if (url.pathname === '/projects' || url.pathname.startsWith('/projects/')) return;

  if (url.pathname.startsWith('/data/')) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(DATA).then((c) => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // The project page carries its project id in the query string; match the
  // cached shell regardless so it opens offline too.
  if (url.pathname === '/project.html') {
    e.respondWith(caches.match('/project.html').then((res) => res || fetch(e.request)));
    return;
  }

  e.respondWith(caches.match(e.request).then((res) => res || fetch(e.request)));
});
