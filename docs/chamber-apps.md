# Chamber apps

Status: **proposal**, being implemented on this branch.

## Why

Chambers ship data, converters, scripts, skills and subagents, but no user
interface of their own. Some work wants one: a guided routine (an exercise
plan that talks the user through each set and logs what happened), a small
form that is quicker than a conversation, a view over the chamber's data.
Putting each of these into `webapp/` would make the dashboard
chamber-specific, which the framework must not be (`docs/contributing.md`,
"No owner specifics in the framework").

A **chamber app** is a Progressive Web App that a chamber ships in its own
repository. It is an **actual PWA**: it installs, starts and works offline
without Retinue. Run under Retinue, it additionally finds `window.Retinue`, a
small client for Retinue services; without Retinue that object is absent and
the app works from the data it carries, keeping what it records on the device.

## Trust

A chamber app has the trust level of the chamber that ships it. Chamber code
already runs in the container (scripts, converters, plugin agents) with more
power than any page, so an app gets no permission system of its own: what it
may do is decided when its directory is reviewed and merged into the chamber
(the app directory is Tier 3 in its chamber). The one rule kept is the
dashboard's Content-Security-Policy on every app page — script only from the
app's own files, no inline script — so data an app displays (a mail quoted in
the store, say) cannot become script.

## What a chamber ships

```
<chamber>/.retinue/apps/<app>/
├── manifest.webmanifest   standard Web App Manifest
├── sw.js                  the app's own service worker (optional, for offline)
├── index.html             entry page
└── …                      scripts, styles, images, data files (JSON, …)
```

The manifest is a plain Web App Manifest (`name`, `short_name`, `icons`,
`start_url: "./"`, `display: "standalone"`). An optional `retinue` member may
carry metadata for the Apps list (`description`, `sphere`), nothing more.

## Where apps run

- **The dashboard** serves each app at `/apps/<chamber>/<app>/` (static files,
  traversal-safe, the dashboard's CSP and sign-in) with a service-worker scope
  of the app's own directory, so each app is installable as its own PWA. An
  *Apps* entry in the navigation lists them (`GET /apps/index.json`) and opens
  them.
- **The Ara Android app** shows the same list inside Ara and opens apps in its
  WebView, with `window.RetinueShell` for speech and notifications (separate
  issue in `ara-android`).
- **Elsewhere** (a static copy, a local file, a dev server) the app runs
  standalone. A browser extension that supplies `window.Retinue` there is a
  later option; the client API below is kept generic, independent of the
  dashboard's internals, so such a host can implement it unchanged.

## The client: `window.Retinue`

An app loads `/apps/retinue.js` (a framework file) and feature-detects
`window.Retinue`. The client talks to a small, stable HTTP API under
`/apps/api/` rather than to dashboard internals:

| Member | Does | Endpoint |
|---|---|---|
| `Retinue.app` | `{chamber, id}` of the running app | — |
| `sparql(query, endpoint = "life")` | read-only query, JSON results | `POST /apps/api/sparql` |
| `submit(name, blob, dir)` | write a file into the app's chamber (`dir` relative to the chamber, default the chamber's inbox); converters / the Archivist pick it up | `PUT /apps/api/files/<chamber>/<dir>/<name>` |
| `ask(agent, text, opts)` | open or continue a dashboard thread addressed to an agent, with attention fields (`importance`, `due`, …); returns the thread id | `POST /apps/api/ask` |
| `schedule(job)` / `unschedule(id)` / `jobs()` | reminders (a notification at a time) and agent jobs (a prompt run by the scheduler, once or recurring) | `/apps/api/schedule` |
| `speak(text, lang)` | speech output | client-side (`speechSynthesis` / the shell) |
| `notify(title, body)` | a notification | client-side / push |

App jobs live in one schedule file per app
(`/root/.retinue/apps/<chamber>/<app>/schedule.json`), which `scheduler.py`
reads beside the chamber manifests; their ids are `app:<chamber>:<app>:<id>`,
and the scheduler's listing shows which app owns each.

## Open points

- Whether a browser extension host is worth building, and when.
- How the *Apps* list orders and badges apps (attention items for an app's
  reminders?).
- Tests: serving and traversal, CSP on app pages, no script type served from
  outside app directories, the `/apps/api/` endpoints, app jobs in the
  scheduler.
