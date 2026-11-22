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
repository. The framework serves it, and offers it a small set of Retinue
services. An app is an **actual PWA**: it installs, starts and works offline
without Retinue. Opened outside Retinue (from a file, from static hosting, in
a browser without the extension below), the Retinue objects are simply
absent, and the app degrades: it works from data it carries and keeps what it
records on the device.

## What a chamber ships

```
<chamber>/.retinue/apps/<app>/
├── manifest.webmanifest   standard Web App Manifest, plus a "retinue" member
├── sw.js                  the app's own service worker (offline)
├── index.html             entry page
└── …                      scripts, styles, images, data files (JSON, …)
```

The `retinue` member of the manifest declares what the app uses:

```json
{
  "name": "…", "short_name": "…", "start_url": "./", "display": "standalone",
  "icons": [ … ],
  "retinue": {
    "api": 1,
    "sparql": ["life"],
    "submit": [{"dir": "observations/inbox/", "types": ["application/json"]}],
    "agents": ["<subagent>", "ara"],
    "schedule": true,
    "speech": true,
    "notify": true
  }
}
```

The app directory is Tier 3 in its chamber (it is code that runs with the
user's authority, see "Trust"): it reaches `main` through a reviewed PR.

## Where apps run

- **On the dashboard's origin**, under `/apps/<chamber>/<app>/`. The gateway
  serves the directory as static files (never a script type from anywhere
  else, `docs/dashboard.md`, "No route serves bytes from outside as a
  script"), with the app's own service-worker scope. No wildcard subdomain is
  needed. Each app installs as its own home-screen icon.
- **From the dashboard**: an *Apps* tab lists the apps of every mounted
  chamber (from their manifests) and opens them. The Android shell opens them
  in its WebView, where speech and notifications come through
  `window.RetinueShell`.
- **On a desktop, elsewhere**: an optional browser extension injects the same
  client object into an app loaded from another origin (a static copy, a
  local file) and forwards its calls to a configured Retinue instance. Without
  the extension the app runs standalone.

## The client object

One framework script, `/apps/retinue.js`, defines `window.Retinue` when the
app runs under Retinue (the extension defines the same object). An app
feature-detects it, member by member, as the dashboard detects
`window.RetinueShell`:

| Member | Does | Backed by |
|---|---|---|
| `sparql(endpoint, query)` | read-only query, JSON results | the existing SPARQL proxy |
| `submit(dir, name, blob)` | write a file into a declared directory of the app's own chamber; converters and the Archivist pick it up as from any inbox | the letterbox mechanism (`docs/scheduling.md`) |
| `ask(agent, text, opts)` | open or continue a dashboard thread addressed to an agent, with attention fields (`importance`, `due`, …); returns the thread id | `conversation-push` semantics |
| `schedule(job)` / `unschedule(id)` | create a reminder (a notification at a time) or an agent job (a prompt run by the scheduler, once or recurring) | `scheduler.py`, jobs kept per app under `/root/.retinue/apps/<chamber>/<app>/schedule.json` |
| `speak(text, lang)` | speech output | `speechSynthesis` / the shell's TTS |
| `notify(title, body)` | a local notification | the shell / Web Notifications |

Agent jobs are included on purpose: an app that can `ask` an agent can spend
model credit anyway, so withholding the scheduler would protect nothing.
Scheduled jobs show on the scheduler's own listing, labelled with their app,
so the user can see and stop them.

## Trust — what the manifest does and does not enforce

An app served on the dashboard's origin **is** the dashboard as far as the
browser is concerned: it shares the sign-in, and a script on that origin can
open any other page of the origin in a window and act in it, including
approving a pending send. Neither `Sec-Fetch-Site` (it only says
"same-origin") nor `Referer` (a same-origin page may set it to any
same-origin URL) can tell an app's request from the dashboard's. So on the
dashboard's origin:

- the `retinue` member of the manifest is a **declaration that review checks**,
  not a sandbox; it is enforced only where the extension or the shell relays
  the calls;
- what keeps foreign content from becoming script inside an app is the same
  **Content-Security-Policy** as the dashboard's (`script-src 'self'`, no
  inline script, no `eval`), applied to every `/apps/` response, plus the rule
  that an app renders data from the store as text, never as markup.

This is the same trust level chamber code already has: a chamber's scripts and
plugin run in the container with more power than any page. The PR review of
the app directory is the gate.

**If isolation is wanted later**, the one change that buys it is a **single
second origin for all apps** (one extra host name or port, not a wildcard),
with `retinue.js` talking to the gateway through a token scoped to the app's
declared manifest. That makes the manifest enforceable on every device, at the
cost of one more proxy entry. The client object stays the same, so apps need
no change when that switch is made.

## Data written by apps

`submit` writes into the app's own chamber only, into directories the manifest
declares, and only file types that chamber's `.inbox.json` accepts there. A
record is one file (e.g. `2026-…-<app>-session.json`); the chamber's converter
turns it into triples, as for Health Connect uploads. Apps get no general file
read or write: what they read comes from the store.

## Open points

- Whether a thread opened by `ask` is always visible to the user, or whether
  an app may also dispatch a subagent job whose answer alone reaches them.
- How the *Apps* tab orders and badges apps (attention items for an app's
  pending reminders?).
- The extension: which browsers, and how it is paired with an instance
  (client certificate vs token).
- Tests: CSP on `/apps/`, no script type from outside, manifest validation,
  scheduler jobs owned by an app.
