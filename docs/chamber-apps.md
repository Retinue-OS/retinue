# Chamber apps

Status: **implemented** — the framework side in this repository (gateway,
client, scheduler, dashboard), the Android side in `ara-android`
(RetinueShell/7). A generic example ships in `examples/chamber-app/`.

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
(the app directory is Tier 3 in its chamber). There are no tokens: any app
page, being on the dashboard's origin and behind its sign-in, may call every
`/apps/api/` endpoint for any app.

The one rule kept is the dashboard's Content-Security-Policy on every app
page — script only from this origin, no inline script, no `eval`, nothing
from another origin — so data an app displays (a mail quoted in the store,
say) cannot become script. App pages get it **enforced** from the start,
whatever `RETINUE_CSP` says, except `off` (the rollback for every page):
report mode exists for the dashboard's own pages to migrate, and an app is
written against the policy from its first line (`content_policy.app_headers`,
the page key `chamber-app` in `PAGE_SOURCES`). App pages may also play their
own sounds (`media-src 'self'`) and show images they draw (`img-src blob:`).
Beyond that the gateway checks shape, not authority: names, paths, sizes, so
nothing an app sends can name a file outside its place.

## What a chamber ships

```
<chamber>/.retinue/apps/<app>/
├── manifest.webmanifest   standard Web App Manifest (required: it is what makes a directory an app)
├── sw.js                  the app's own service worker (optional, for offline)
├── index.html             entry page
└── …                      scripts, styles, images, sounds, data files (JSON, …)
```

The manifest is a plain Web App Manifest (`name`, `short_name`, `icons`,
`start_url: "./"`, `display: "standalone"`). An optional `retinue` member
carries metadata for the Apps list — `description`, `sphere` (the default
sphere of its reminders) — nothing more. Chamber and app names are one path
segment each: letters, digits, `_` and `-`, starting with a letter or digit.
A chamber whose name starts with `_`, or is `api`, has no apps served.

## Where apps run

- **The dashboard** serves each app at `/apps/<chamber>/<app>/`
  (`/apps/<chamber>/<app>` redirects there, so relative links resolve).
  Only files inside the app directory are served, after symlinks are
  resolved; no path segment may start with a dot, and a directory without a
  manifest is no app. HTML, JavaScript (`.js`, `.mjs`), CSS, JSON, the
  manifest, images (PNG, SVG, JPEG, WebP, GIF, ICO), sounds (MP3, Ogg, WAV,
  M4A), text and CSV are served as their types under `nosniff`; any other
  suffix is a download. A service worker at the app's root has the app's
  directory as its scope, so each app installs as its own PWA. The
  dashboard's own service worker leaves `/apps/` alone (network only); the
  *Apps* page (`/apps.html`, a tab in the navigation row) is part of the
  dashboard's shell and lists `GET /apps/index.json`, keeping the last list
  on the device for offline use.
- **The Ara Android app** shows the same list inside Ara (its own Apps
  screen, from the endpoint's `/apps/index.json`) and opens apps in its
  WebView, where `window.RetinueShell` (RetinueShell/7) adds native speech,
  notifications and local reminder alarms. See `ara-android`'s README,
  "Chamber apps".
- **Elsewhere** (a static copy, a local file, a dev server) the app runs
  standalone: `/apps/retinue.js` does not load, `window.Retinue` is absent. A
  browser extension that supplies `window.Retinue` there is a later option
  (`retinue.js` leaves an object that is already there alone); the client API
  below is kept generic, independent of the dashboard's internals, so such a
  host can implement it unchanged.

`GET /apps/index.json`:

```json
{"apps": [{"chamber": "routines", "id": "routine", "name": "Routine timer",
           "short_name": "Routine", "description": "…", "sphere": "health",
           "url": "/apps/routines/routine/", "start_url": "/apps/routines/routine/",
           "icons": [{"src": "/apps/routines/routine/icon.svg", "sizes": "any",
                      "type": "image/svg+xml"}], "theme_color": "#0b0d12"}]}
```

Icon and start URLs are resolved against the app and dropped when they point
outside it.

## The client: `window.Retinue`

An app loads `/apps/retinue.js` (a framework file, a classic script, so the
object exists before the app's module runs) and feature-detects
`window.Retinue`. The client talks to a small, stable HTTP API under
`/apps/api/` rather than to dashboard internals. Every call returns a
Promise; an HTTP error rejects with an `Error` carrying `status` and the
server's reason.

| Member | Does | Endpoint |
|---|---|---|
| `Retinue.version` | the client API's version, `1` | — |
| `Retinue.app` | `{chamber, id}` of the running app | — |
| `Retinue.shell` | `{version, app, platform}` inside the Android shell (`version`: the bridge, from the user agent's `RetinueShell/<n>`; `app`: the app's release), else `null` | — |
| `sparql(query, endpoint = "life")` | read-only query, SPARQL JSON results; `endpoint` is `life` or the name of a deployment's `SPARQL_ENDPOINT_<NAME>`; updates and `SERVICE` are refused | `POST /apps/api/sparql` `{query, endpoint}` |
| `submit(name, blob, dir, {replace})` | write a file into the app's chamber (`dir` relative to the chamber, default the chamber's first inbox in `.inbox.json`); converters / the Archivist pick it up | `PUT /apps/api/files/<chamber>/<dir>/<name>` (one segment: the inbox) |
| `ask(agent, text, opts)` | open (or, with `opts.thread`, continue) a dashboard thread addressed to an agent, with attention fields (`importance`, `due`, `lead`, `sphere`, `tags`, `kind`); resolves to the thread id | `POST /apps/api/ask` |
| `schedule(job)` / `unschedule(id)` / `jobs()` | reminders (a notification at a time) and agent jobs (a prompt run by the scheduler, at calendar times or on an interval) | `POST` / `DELETE …/<id>` / `GET /apps/api/schedule/<chamber>/<app>` |
| `done(id)` / `skip(id)` / `snooze(id, minutes)` | a reminder's session done, "not today", "again in a while" | `POST /apps/api/schedule/<chamber>/<app>/<id>/done\|skip\|snooze` |
| `speak(text, lang)` | speech output; resolves `true` once spoken | client-side (the shell's native TTS, else `speechSynthesis`) |
| `notify(title, body)` | an immediate notification **while the app is open** ("next set in 10 s"); not a reminder — see below | client-side (the shell, else the Notification API) |

**Files** (`submit`). Refused: an unknown chamber or one starting with `_`; a
path segment starting with a dot anywhere (`.retinue/` holds the chamber's
apps, plugin and manifests — a file written there could become a script the
dashboard serves; `.git/` likewise); `..`; a symlinked directory leading out
of the chamber; a file name outside letters, digits and ` ._()+,=@-`. An
existing file is replaced only with `{replace: true}` (`?replace=1`; else
`409`); a body is limited to `APPS_FILE_MAX_BYTES` (25 MiB) and needs a
`Content-Length`. A chamber without an inbox asks for a `dir` (`400`).

**Ask.** The thread is the user's own — the app speaks for them as the
composer does — with an agent-only note naming the app and the agent to
route to. Its messages never count as via the edge, so no turn an app starts
may type on the paired device.

**Same-origin.** Every call that changes something (`POST`, `PUT`, `DELETE`)
refuses a request another site's page sent (`Sec-Fetch-Site`), as the
conversation endpoints do. Native callers (the Android shell) send no such
header and are not affected.

### Jobs

`schedule(job)` registers a job, replacing one of the same `id` (its snoozes
and skips go with it), so an app re-declares its whole plan whenever the plan
changes. Two kinds:

```js
Retinue.schedule({
  kind: "reminder", id: "hand-1",
  at: "10:30", days: "mon-sun",          // or "mon,wed,fri"; or date: "2026-12-01"
  tz: "Europe/Zurich",                    // default: the instance's zone
  title: "Hand session 1", body: "Imagine, then try — about 10 min",
  open: "#session/hand-1",                // fragment or path inside the app
  importance: 3, sphere: "health", tag: ["training"],
  lead: "30m"                             // how late it may still fire, and how long it may wait if held
});
Retinue.schedule({ kind: "job", id: "weekly", prompt: "Summarise the week's sessions.",
                   at: "18:00", days: "fri" });   // or interval_seconds (≥ 300)
```

Checked on the way in (`chamber_apps.normalize_job`): `id` 1–64 of
`A–Z a–z 0–9 . _ -`; the calendar fields as the scheduler reads them
(`docs/scheduling.md`, "Calendar triggers"); a reminder needs a `title`
(≤ 120 characters; `body` ≤ 500); `open` is a fragment or a relative path
with no scheme, no leading slash and no `..`; `importance` 0–5; a job needs a
`prompt` (≤ 4000 characters) and never runs a `command`. At most 200 jobs per
app. A reminder costs no model credit: no agent runs.

App jobs live in one schedule file per app,
`$CHAMBER_APPS_STATE_DIR/<chamber>/<app>/schedule.json` (default
`/root/.retinue/apps/…`, outside the chambers), written by the gateway and
read by `scheduler.py` beside the chamber manifests; their ids are
`app:<chamber>:<app>:<id>`, and `python3 scripts/scheduler.py --list` shows
each job with its trigger, next run and owner (`app <chamber>/<app>`). A
prompt job runs as a fresh `claude -p` like any scheduled prompt, its prompt
prefixed with the app and job it comes from.

## Reminders

A reminder must arrive when the app is closed, so it is fired by the server,
never by the page: browsers cannot wake a closed page at a set time.

**Timing.** The scheduler fires each slot of the reminder's calendar trigger
(`docs/scheduling.md`, "Calendar triggers": weekdays or one-off dates, an
IANA zone, DST resolved by the zone). Firing is one request to the gateway,
`POST /internal/apps/reminders/fire` (token-gated like
`/internal/conversations`). A missed slot (container down) fires once on
start if it is still within its `lead` (default 15 min), otherwise it is
skipped; a slot whose request failed is retried on the next tick while
within its lead.

**What a reminder is.** Not a thread: an **attention item** of kind
`reminder` on the home list (`reminder:app:<chamber>:<app>:<id>`) — title,
body, the app's link — delivered by the attention model like any other item.
Its importance, sphere (the job's, else the app manifest's
`retinue.sphere`, else `admin`) and tags decide whether it rings now in the
current focus mode or waits for the next digest. Its deadline is the slot
plus `lead`, with `lead` as its lead time, so a held reminder climbs as the
lead runs out — that is how `lead` bounds the wait. There is one item per
reminder: its latest occurrence. It is cleared (marked done, `done_how`
saying why) when

- the user opens it (`opened`: the page reports it, below),
- the app reports the session done (`Retinue.done(id)`) — with no item
  open, *done* ahead of time skips the next occurrence when it falls today,
- `skip(id)`: "not today" — nothing more fires on today's date, today's
  snoozes included,
- `snooze(id, minutes)` (default 15): one extra one-off occurrence; the item
  is cleared until it fires,
- the user marks it done on the home list, like any item,
- the reminder is unscheduled,
- or its next occurrence replaces it (the record keeps `replaced`),

so reminders never pile up. The records are `ATTENTION_DIR/reminders.json`
(`chamber_apps.ReminderStore`); the life-store emit names them
`urn:retinue:reminder:<id>`.

**Opening.** Tapping the notification opens the app at `open`: the push
payload's URL is the app's deep link,
`/apps/<chamber>/<app>/?retinue-reminder=<id>#session/hand-1`, which the
dashboard's service worker opens on `notificationclick` (same origin, so no
new rule is needed); the home list's row links the same URL. On load,
`retinue.js` sees the `retinue-reminder` parameter, posts
`…/<id>/opened` and drops the parameter from the address
(`history.replaceState`), so a reload or a bookmark reports nothing again.
`GET` never changes the item.

**Actions.** The push carries `kind: "reminder"`, `api`
(`/apps/api/schedule/<chamber>/<app>/<id>`) and two actions: *In 15 min*
(`snooze`) and *Skip today* (`skip`). The service worker shows only those
two, and posts `<api>/snooze` or `<api>/skip` without opening anything; it
accepts only an `api` of exactly that shape.

**Android.** Web Push does not reach the shell's WebView. The Ara app
therefore fetches the reminders of its endpoint's apps,
`GET /apps/api/schedule?arm=1`, and arms them locally with Android alarms,
so they also fire offline:

```json
{"now": "2026-11-24T09:00:00Z", "horizon_hours": 48, "snooze_minutes": 15,
 "reminders": [{"id": "app:routines:routine:hand-1", "chamber": "routines",
   "app": "routine", "job": "hand-1", "title": "Hand session 1", "body": "…",
   "url": "/apps/routines/routine/?retinue-reminder=hand-1#session/hand-1",
   "importance": 3, "sphere": "health",
   "occurrences": [{"at": "2026-11-24T09:30:00Z", "ring": true}, …],
   "open": null, "delivered": []}]}
```

An occurrence is named by its instant in UTC to the second with a `Z`
(`calendar_trigger.occurrence_key`), the same spelling everywhere. `ring`
says whether the occurrence would break through the focus mode scheduled for
its time; the shell shows the others quietly. `arm=1` records that a shell
arms reminders: for `APPS_SHELL_ARM_WINDOW_HOURS` (36) afterwards, a
reminder that is to ring waits `APPS_SHELL_GRACE_SECONDS` (90) before the
server pushes it to the other devices. The shell confirms each occurrence it
showed, `POST …/<id>/delivered {"occurrence"}`, before or after the server
fired it; a confirmed occurrence is never pushed by the server (it stays on
the home list). With no confirmation in time the push goes out as usual. The
notification's own *In 15 min* and *Skip today* post `snooze` (with the
exact `at` the shell armed) and `skip`; tapping it opens the deep link in
the shell's WebView. `Retinue.schedule` and its siblings call
`RetinueShell.remindersChanged()` where the shell has it, so a changed plan
is re-armed at once rather than on the next fetch.

**Standalone.** Outside Retinue an app can remind only while it is open (its
own timers and the Notification API). It should say so rather than promise
reminders it cannot deliver — the example app does.

## Configuration

| Variable | Default | |
|---|---|---|
| `CHAMBER_APPS_STATE_DIR` | `/root/.retinue/apps` | the apps' schedule files (gateway writes, scheduler reads) |
| `APPS_FILE_MAX_BYTES` | 26214400 | the largest file `submit` takes |
| `APPS_SHELL_ARM_WINDOW_HOURS` | 36 | how long a shell's `arm=1` counts |
| `APPS_SHELL_GRACE_SECONDS` | 90 | how long a ringing reminder waits for a shell's confirmation (0: never) |
| `APPS_REMINDER_FIRE_URL` | `http://localhost:$WEB_GATEWAY_PORT/internal/apps/reminders/fire` | where the scheduler fires reminders |

## Code and tests

- `scripts/chamber_apps.py` — discovery, the index, path checks for serving
  and `submit`, schedule files, job validation, snooze/skip, deep links, the
  reminder records.
- `scripts/calendar_trigger.py` — calendar triggers (shared with every
  scheduler job).
- `scripts/web-gateway.py` — the routes (`_serve_apps_get`,
  `_handle_apps_post|put|delete`, `_handle_internal_reminder_fire`) and the
  reminder lifecycle (`_reminder_fire`, `_reminder_action`,
  `_reminders_tick`, `_reminders_for_shell`).
- `scripts/scheduler.py` — calendar slots, the missed-slot rule, app jobs,
  `--list`.
- `webapp/apps/retinue.js`, `webapp/apps.html` + `components/apps.js`, the
  reminder handling in `webapp/sw.js`.
- Tests: `tests/test_chamber_apps.py` (serving and traversal, the policy on
  app pages, no script type outside app directories, the files API's path
  safety, the schedule API, the reminder lifecycle, snooze, skip, the shell's
  listing and confirmation, ask, sparql), `tests/test_scheduler_calendar.py`
  (zones, DST, missed slots, app jobs), `tests/test_webapp_sw_reminders.py`,
  `tests/test_webapp_apps_client.py`; the policy text in
  `tests/test_web_gateway_csp.py`.

## Open points

- Whether a browser extension host is worth building, and when.
- How the *Apps* list orders and badges apps: today alphabetically by
  chamber and app, without badges; an app's open reminder could badge it.
- Whether the shell should also arm *agent* jobs locally (no: those run on
  the server; only reminders need the phone).
- `script-src 'self'` lets an app page load any script of the origin, not
  only its own files and `/apps/retinue.js`. A path-restricted source needs
  the public host in the policy (CSP has no path form of `'self'`), which
  the gateway does not reliably know behind the proxy. Accepted: every
  script on the origin is reviewed framework or chamber code, and nothing a
  sender chose is served as a script (`tests/test_web_gateway_no_script_types.py`,
  `tests/test_chamber_apps.py`).
- App pages are enforced even in report mode (see "Trust") — the safer
  reading of "the dashboard's policy on every app page".
- An app cannot declare a reminder `critical`: the attention model's top
  level stays the agents' and the user's. A medication reminder that must
  ring in every mode is a case for adding it.
- Delivery to a shell is best effort around the grace: if the server fires
  a ringing reminder before the shell has fetched (`arm=1`) for 36 hours,
  the other devices get a push and the phone shows nothing until its next
  fetch; and a phone that shows an occurrence after the grace ran out
  doubles the server's push on the other devices.
