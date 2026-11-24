# Example chamber app: Routine timer

A minimal chamber that ships one **chamber app** (docs/chamber-apps.md): a
Progressive Web App that talks the user through a timed routine, step by
step, logs every session, and keeps a daily reminder.

```
examples/chamber-app/
├── .inbox.json                      the inbox the app's session logs land in
├── inbox/
└── .retinue/apps/routine/
    ├── manifest.webmanifest         a plain Web App Manifest (+ "retinue")
    ├── index.html                   loads /apps/retinue.js, then app.js
    ├── app.js                       the app; feature-detects window.Retinue
    ├── app.css
    ├── routine.json                 the routine: steps, what to say, how long
    ├── icon.svg
    └── sw.js                        its own service worker (offline)
```

**Under Retinue** — mount the directory as a chamber, e.g. in the
deployment's `chambers.json`:

```json
{"name": "routines", "path": "examples/chamber-app"}
```

and the app appears in the dashboard's *Apps* list and in the Ara Android
app, served at `/apps/routines/routine/`. There it speaks through
`Retinue.speak` (native text-to-speech in the Android shell), files each
session with `Retinue.submit` as a JSON file in `inbox/`, and sets its daily
reminder with `Retinue.schedule` — fired by the server, as an attention item
of kind `reminder`, so it arrives while the app is closed; finishing a
session reports it done.

**On its own** — serve the `routine/` directory from any static server (or
open a copy): `window.Retinue` is absent, the app runs from its own files,
keeps the log on the device and says that it cannot remind while closed.

Nothing in it is specific to a person or a deployment; change
`routine.json` to make it another routine.
