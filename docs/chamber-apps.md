# Chamber apps — design proposal

Status: **proposal**, for discussion in its PR. Nothing here is implemented yet.

## Why

Chambers ship data, converters, scripts, skills and subagents, but no user
interface of their own. Some work wants one: a guided routine (an exercise
plan that talks the user through each set and logs what happened), a small
form that is quicker than a conversation, a view over the chamber's data.
Putting each of these into `webapp/` would make the dashboard
chamber-specific, which the framework must not be (`docs/contributing.md`,
"No owner specifics in the framework").

A **chamber app** is a Progressive Web App that a chamber ships in its own
repository. It is an **actual PWA**: it starts and works offline without
Retinue. Run by a Retinue **host** (the browser extension, the Ara Android
app), it additionally gets `window.Retinue`, a small client for Retinue
services. Without a host that object is absent, and the app works from the
data it carries and keeps what it records on the device.

## Principle: security does not depend on where the app is loaded from

An app never runs **as** the dashboard's origin. A page with that origin
shares the dashboard's sign-in and can act in any of its pages (approving a
pending send included); no header tells such a page's requests from the
dashboard's own. A permission list that is only a promise is not a
permission. (The dashboard still runs apps — in a sandboxed frame with an
opaque origin, see "Hosts".)

So every app call reaches Retinue **through a host, with a capability token
bound to that app**, and the gateway enforces the app's granted capabilities
on every call, whatever origin, device or path the app was loaded from:

- the app runs on an origin of its own (an opaque origin in the dashboard's
  sandboxed frame, the extension's, the Android app's per-app WebView origin,
  or wherever it is hosted);
- the host holds the token; the app's own script never sees it;
- the gateway's app API accepts **only** such tokens, never the dashboard's
  sign-in, and refuses anything beyond the token's capabilities;
- the endpoints that act with the user's authority (send approval, sign-in,
  settings) accept no app token at all.

Three hosts share this one path: **the dashboard**, the **browser
extension** and the **Ara Android app**. The extension is the test that keeps
the path honest: it cannot borrow the dashboard's sign-in, so nothing works
unless the token path is complete.

## What a chamber ships

```
<chamber>/.retinue/apps/<app>/
├── manifest.webmanifest   standard Web App Manifest, plus a "retinue" member
├── sw.js                  the app's own service worker (offline, standalone use)
├── index.html             entry page
└── …                      scripts, styles, images, data files (JSON, …)
```

```json
{
  "name": "…", "short_name": "…", "start_url": "./", "display": "standalone",
  "icons": [ … ],
  "retinue": {
    "api": 1,
    "sparql": ["life"],
    "submit": [{"dir": "observations/inbox/", "types": ["application/json"]}],
    "agents": ["<subagent>", "ara"],
    "schedule": ["reminder", "agent"],
    "speech": true,
    "notify": true
  }
}
```

The app directory is Tier 3 in its chamber: it reaches `main` through a
reviewed PR. Review is the first gate; the token is the second.

## How a host gets and runs an app

1. **Catalogue.** `GET /apps/index.json` (dashboard sign-in or a host token)
   lists the apps of every mounted chamber: chamber, id, name, icons, version
   (content hash), declared capabilities.
2. **Bundle.** `GET /apps/bundle/<chamber>/<app>` returns the app's files,
   served as data (a download), never as a page on the dashboard's origin.
3. **Grant.** The first time the user opens an app, the host shows the
   declared capabilities (which store it reads, which chamber folder it
   writes, which agents it may address, whether it may schedule agent jobs)
   and asks once. On consent the host obtains an **app token** from the
   gateway (`POST /apps/grant`, an authenticated host request). The token
   names the app, its manifest hash and the granted capabilities. A changed
   manifest (new hash) needs a new grant; the user can revoke a grant on the
   dashboard.
4. **Run.** The host serves the bundle from its own origin
   (`chrome-extension://…/apps/<chamber>/<app>/`, the Android app's per-app
   WebView origin) and injects `window.Retinue`, whose calls go through the
   host to `/apps/api/…` with the token.

An app hosted elsewhere (a static copy, a developer's local server) gets the
same object from the extension, for an origin the user has allowed for that
app.

## The client object

Identical in every host, feature-detected member by member:

| Member | Does | Gateway side |
|---|---|---|
| `sparql(endpoint, query)` | read-only query, JSON results | the existing SPARQL proxy, endpoints limited to the grant |
| `submit(name, blob, dir?)` | write a file into a granted directory of the app's own chamber; converters or the Archivist pick it up | the letterbox mechanism (`docs/scheduling.md`), type and size checked |
| `ask(agent, text, opts)` | open or continue a dashboard thread addressed to a granted agent, with attention fields; returns the thread id | `conversation-push` semantics, thread marked as from the app |
| `schedule(job)` / `unschedule(id)` / `jobs()` | reminders (a notification at a time) and agent jobs (a prompt run by the scheduler, once or recurring) | `scheduler.py`, jobs per app under `/root/.retinue/apps/<chamber>/<app>/schedule.json`, ids `app:<chamber>:<app>:<id>`, listed with their app |
| `speak(text, lang)` | speech output | host-local (extension TTS, native TTS) |
| `notify(title, body)` | a notification | host-local |
| `storage.get/set/remove(key)` | small per-app key-value store | host-local (the dashboard host keeps it in its own IndexedDB, keyed per app) |

Agent jobs are included: an app that may `ask` an agent can spend model credit
anyway, so withholding the scheduler protects nothing. They are listed with
their app on the scheduler's listing and stop when the grant is revoked.

## Hosts

- **The dashboard.** An *Apps* tab lists the apps; opening one shows it in an
  `<iframe sandbox="allow-scripts">` — **no** `allow-same-origin`, so the
  frame has an opaque origin: no access to the dashboard's cookies, storage
  or DOM, no top-level navigation, no pop-ups. The app's files are served
  from `/apps/run/<chamber>/<app>/…` with a response header
  `Content-Security-Policy: sandbox allow-scripts` plus the dashboard's
  script policy, so they stay sandboxed even when opened directly as a page.
  The dashboard page around the frame is the host: it injects nothing into
  the frame, but answers the app's `postMessage` requests (the `retinue.js`
  shim the app loads speaks this protocol when it finds itself framed),
  checks the message source is that frame, and relays to `/apps/api/…` with
  the app's token, which the frame never sees. A request the frame sends to
  the gateway by itself arrives cross-site and without a token, and is
  refused. Opaque origins get no service worker or persistent storage, so
  inside the dashboard the host offers `Retinue.storage` (a small per-app
  key-value store kept on the device by the host page); standalone the app
  uses its own storage and service worker.
- **Browser extension.** Pairs once with a Retinue instance, lists its apps,
  runs them from the extension's origin (or injects the client into an
  allowed origin where an app is hosted elsewhere), relays calls with the app
  token, shows consent and revocation.
- **Ara Android app.** Ara has no home screen of its own, so chamber apps
  appear inside Ara as apps alongside the dashboard: a list to open them
  from, each in its own WebView origin with the same client, speech and
  notifications native. Tracked in the `ara-android` repository.

All three show the same consent sheet on first open and use the same grant
(`POST /apps/grant`, the dashboard's own authenticated request), revocable
on the dashboard.

## Data written by apps

`submit` writes into the app's own chamber only, into directories granted from
the manifest, and only file types that chamber accepts there. A record is one
file; the chamber's converter turns it into triples, as for Health Connect
uploads. Apps get no general file read or write: what they read comes from the
store.

## Open points

- Whether a thread opened by `ask` is always visible to the user, or an app may
  also dispatch a subagent job whose answer alone reaches them.
- Token form (opaque with a server-side record, or signed), lifetime, refresh.
- Bundle format (archive or file list) and update check (content hash).
- How the extension pairs: client certificate, or a one-time pairing code
  shown on the dashboard.
- Tests: grants and capability enforcement per call, app token refused on the
  authority endpoints, dashboard sign-in refused on the app API, bundle served
  as data only, app-owned scheduler jobs removed on revocation.
