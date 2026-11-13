// The /sync-health page's script (sync-health.html; docs/dashboard.md,
// "Inside the Android shell"). A module of its own where it used to be an
// inline module at the end of the page, in the same place in the load order:
// the pages' Content-Security-Policy runs no inline script (docs/dashboard.md,
// "The Content-Security-Policy").

import {
  inShell, healthConnectAvailable, healthSync, healthSyncStatus, onHealthSync,
  describeHealthSync, describeHealthSyncEvent, healthUploadEnabled,
} from '/components/shell.js';

// The Ara app's applicationId (ara-android, app/build.gradle.kts): the
// intent link names the package so Android opens that app and no other.
const ANDROID_PACKAGE = 'os.retinue.ara';
// How long "triggered" stays on screen before the page goes back, and how
// long a finished update does.
const LEAVE_AFTER_MS = 2500;
const LEAVE_AFTER_DONE_MS = 1200;

const line = document.querySelector('[data-line]');
const explain = document.querySelector('[data-explain]');
const actions = document.querySelector('[data-actions]');
const nowBtn = document.querySelector('[data-now]');
const openApp = document.querySelector('[data-open-app]');
const backBtn = document.querySelector('[data-back]');
const show = (el) => { el.hidden = false; actions.hidden = false; };
const say = (text) => { line.textContent = text; };

// Back to where the link was tapped (the thread, in place in the shell),
// or home when this page was opened cold.
const leave = () => {
  if (history.length > 1) history.back();
  else location.replace('/');
};

const wantsSync = location.hash === '#sync';
// The instruction is spent once read: a reload, or coming forward to this
// page again, must not start another sync.
if (wantsSync) history.replaceState(history.state, '', location.pathname + location.search);

if (inShell()) {
  const canSync = typeof window.RetinueShell.healthSync === 'function';
  if (!canSync) {
    explain.textContent = 'This version of the Ara app cannot send health data yet. '
      + 'Update the app, then try again.';
  } else if (!healthConnectAvailable()) {
    explain.textContent = 'Health Connect is not available on this phone, so the Ara '
      + 'app has no health data to send.';
  } else if (!healthUploadEnabled(healthSyncStatus())) {
    // The endpoint's own switch (RetinueShell/3): nothing is read or sent
    // for this server until it is turned on in the app's endpoint editor.
    explain.textContent = 'Health data is not uploaded to this Retinue: the switch for this '
      + 'endpoint is off in the Ara app. Turn it on there (Settings \u203a Phone shell \u203a '
      + 'Edit endpoints) and come back.';
  } else {
    let timer = null;
    let settled = false;
    const leaveIn = (ms) => { clearTimeout(timer); timer = setTimeout(leave, ms); };
    const start = () => {
      settled = false;
      nowBtn.disabled = true;
      if (!healthSync()) { say('The Ara app could not start the update.'); nowBtn.disabled = false; return false; }
      say('Health data update triggered.');
      return true;
    };
    onHealthSync((d) => {
      if (settled || d.state === 'started') return;
      settled = true;
      nowBtn.disabled = false;
      say(describeHealthSyncEvent(d));
      if (d.state === 'done') {
        if (wantsSync) leaveIn(LEAVE_AFTER_DONE_MS);
      } else {
        // A failure or a skipped update stays on screen until the user
        // has seen it: no going back on its own.
        clearTimeout(timer);
        show(nowBtn);
        show(backBtn);
      }
    });
    nowBtn.addEventListener('click', start);
    backBtn.addEventListener('click', leave);
    if (wantsSync) {
      if (start()) leaveIn(LEAVE_AFTER_MS);
      else { show(nowBtn); show(backBtn); }
    } else {
      say(describeHealthSync(healthSyncStatus()));
      show(nowBtn);
    }
  }
} else if (/Android/i.test(navigator.userAgent)) {
  // An Android browser: the app can open this very page, sync instruction
  // and all. Built from where this page is served — never configured — and
  // the app opens only a URL on the origin it is set up for.
  const target = `${location.origin}/sync-health#sync`;
  const fallback = `${location.origin}/sync-health`;
  openApp.href = `intent://open?url=${encodeURIComponent(target)}`
    + `#Intent;scheme=retinue-shell;package=${ANDROID_PACKAGE}`
    + `;S.browser_fallback_url=${encodeURIComponent(fallback)};end`;
  explain.textContent = 'Health data from the phone’s Health Connect reaches your '
    + 'health records through the Ara app, not through the browser. Open this page '
    + 'in the Ara app to update it.';
  show(openApp);
} else {
  explain.textContent = 'Health data from the phone’s Health Connect reaches your '
    + 'health records through the Ara app on Android. Open this link on the phone, '
    + 'in the Ara app, to update it.';
}
