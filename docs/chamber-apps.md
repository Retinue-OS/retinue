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
| `notify(title, body)` | an immediate notification **while the app is open** ("next set in 10 s"); not a reminder — see below | client-side (Notification API / the shell) |

App jobs live in one schedule file per app
(`/root/.retinue/apps/<chamber>/<app>/schedule.json`), which `scheduler.py`
reads beside the chamber manifests; their ids are `app:<chamber>:<app>:<id>`,
and the scheduler's listing shows which app owns each.

## Reminders

A reminder must arrive when the app is closed, so it is fired by the server,
never by the page: browsers cannot wake a closed page at a set time.

**Declaring.** The app registers each reminder with `Retinue.schedule`:

```js
Retinue.schedule({
  kind: "reminder", id: "hand-1",
  at: "10:30", days: "mon-sun",          // or "mon,wed,fri"; or date: "2026-12-01"
  tz: "Europe/Zurich",                    // default: the instance's zone
  title: "Hand session 1", body: "Imagine, then try — about 10 min",
  open: "#session/hand-1",                // fragment or path inside the app
  importance: 3, sphere: "health", tag: ["training"],
  lead: "30m"                             // how long it may wait if held
});
```

Re-registering the same `id` replaces it, so an app re-declares its whole
plan whenever the plan changes; `unschedule(id)` removes one. A reminder
costs no model credit: no agent runs.

**Timing.** The scheduler gains calendar triggers next to
`interval_seconds`: a time of day with weekdays (`at` + `days`), or a one-off
`date` + `at`, in an IANA zone (DST resolved by the zone). These triggers are
available to chamber `.schedule.json` jobs too, not only to apps. A missed
slot (container down) fires once on start if it is still within its `lead`,
otherwise it is skipped.

**What a reminder is.** Not a thread: an **attention item** of kind
`reminder` on the home list — title, body, the app's link — delivered by the
attention model like any other item. Its importance, sphere and tags decide
whether it rings now in the current focus mode or waits for the next digest;
`lead` bounds the wait. The item is cleared when the user opens it, when the
app reports the session done (`Retinue.done(id)`, or `skip(id)` for "not
today"), or when its next occurrence replaces it, so reminders never pile up.

**Opening.** Tapping the notification opens the app at `open` (e.g.
`/apps/<chamber>/<app>/#session/hand-1`), not a dashboard thread: the push
payload carries the app URL, and the dashboard's service worker opens it on
`notificationclick` (same origin, so no new rule is needed).

**Actions.** The notification carries two actions: *In 15 min* (snooze: one
extra one-off occurrence) and *Skip today*. The service worker posts them to
`/apps/api/schedule/<id>/snooze|skip` without opening anything.

**Android.** Web Push does not reach the shell's WebView. The Ara app
therefore fetches the reminders of its apps (`GET /apps/api/schedule`) and
arms them locally with Android alarms, so they also fire offline; the server
marks an occurrence delivered when the shell confirms, and does not push it a
second time. Tracked in the `ara-android` issue.

**Standalone.** Outside Retinue an app can remind only while it is open (its
own timers and the Notification API). It should say so rather than promise
reminders it cannot deliver.

## Open points

- Whether a browser extension host is worth building, and when.
- How the *Apps* list orders and badges apps (attention items for an app's
  reminders?).
- Whether the shell should also arm *agent* jobs locally (no: those run on
  the server; only reminders need the phone).
- Tests: serving and traversal, CSP on app pages, no script type served from
  outside app directories, the `/apps/api/` endpoints, app jobs in the
  scheduler, calendar triggers (zones, DST, missed slots), reminder items
  cleared by done/skip/next occurrence, snooze and skip actions.
