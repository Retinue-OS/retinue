// The settings page's own wiring (settings.html): the per-device values, the
// Phone shell card of an app without a settings page of its own, and the way
// into the app's page where it has one. A module of its own where it used to
// be an inline module at the end of the page, in the same place in the load
// order: the pages' Content-Security-Policy runs no inline script
// (docs/dashboard.md, "The Content-Security-Policy").

import { shelfSize, setShelfSize, subscribe } from '/components/shelf-store.js';
import { voiceTimeouts, setVoiceTimeouts } from '/components/continuous.js';
import {
  inShell, openShellSettings, healthConnectAvailable, healthSync, healthSyncStatus,
  setHealthSyncInterval, setHealthAutoSync, onHealthSync, describeHealthSync,
  describeHealthSyncEvent, healthUploadEnabled,
  hasShellEndpoints, shellEndpoints, selectShellEndpoint, onShellEndpoints,
  hasShellSettingsPage,
} from '/components/shell.js';

// Inside an Android app with a settings page of its own (RetinueShell/4):
// everything of the phone is there, including the per-device values this
// page would otherwise show (the app holds them for every endpoint), so
// only the way there and this instance's settings remain here.
const appPage = hasShellSettingsPage();
if (appPage) {
  document.querySelector('[data-app-settings]').hidden = false;
  document.querySelector('[data-app-settings-open]').addEventListener('click', () => openShellSettings());
  for (const el of document.querySelectorAll('[data-device-group], [data-device-card]')) el.hidden = true;
}

// The shelf size (components/shelf-store.js): kept per device and browser
// (inside the app: by the app, for every endpoint); lowering it makes room
// at once, quiet items first.
const input = document.querySelector('[data-shelf-size]');
const show = () => { if (document.activeElement !== input) input.value = String(shelfSize()); };
input.addEventListener('change', () => { input.value = String(setShelfSize(input.value)); });
subscribe(show);
show();

// The voice timeouts (components/continuous.js): per device; shown in
// seconds, kept in milliseconds, clamped to what the loop accepts.
const silence = document.querySelector('[data-voice-silence]');
const noSpeech = document.querySelector('[data-voice-nospeech]');
const showVoice = (t) => {
  silence.value = String(t.silenceMs / 1000);
  noSpeech.value = String(t.noSpeechMs / 1000);
};
silence.addEventListener('change', () =>
  showVoice(setVoiceTimeouts({ silenceMs: Number(silence.value) * 1000 })));
noSpeech.addEventListener('change', () =>
  showVoice(setVoiceTimeouts({ noSpeechMs: Number(noSpeech.value) * 1000 })));
showVoice(voiceTimeouts());

// An app without a page of its own (RetinueShell/3 and older): the Phone
// shell card as before — its settings, and from RetinueShell/3 the
// endpoint menu (the shell's list, under the instances' own names;
// changing the choice switches, so this page is left for the other
// server's) and the way into the shell's editor.
if (inShell() && !appPage) {
  document.querySelector('[data-shell-settings]').hidden = false;
  document.querySelector('[data-shell-open]').addEventListener('click', () => openShellSettings());
  if (hasShellEndpoints()) {
    const pick = document.querySelector('[data-endpoint-pick]');
    const select = document.querySelector('[data-endpoint-select]');
    const edit = document.querySelector('[data-shell-endpoints]');
    edit.hidden = false;
    edit.addEventListener('click', () => openShellSettings('endpoints'));
    const renderEndpoints = () => {
      const list = shellEndpoints();
      pick.hidden = list.length === 0;
      select.disabled = false;
      select.replaceChildren();
      for (const e of list) {
        const opt = document.createElement('option');
        opt.value = e.id; opt.textContent = e.label; opt.selected = e.current;
        select.appendChild(opt);
      }
    };
    select.addEventListener('change', () => {
      const id = select.value;
      const current = shellEndpoints().find((e) => e.current);
      if (!id || (current && current.id === id)) return;
      select.disabled = true;
      if (!selectShellEndpoint(id)) renderEndpoints();
    });
    onShellEndpoints(renderEndpoints);
    renderEndpoints();
  }
}

// Health Connect, where an app without a page of its own has it (with one,
// health data is on the app's page, next to the endpoint's switch;
// components/shell.js): the last
// sync from healthSyncStatus(), kept current by the shell's health-sync
// events; the button starts a deliberate sync; the toggle and interval
// are the shell's own settings, re-read after every write so what shows
// is what the shell holds.
if (inShell() && !appPage && healthConnectAvailable()) {
  const box = document.querySelector('[data-health-sync]');
  const controls = document.querySelector('[data-health-controls]');
  const disabled = document.querySelector('[data-health-disabled]');
  const now = document.querySelector('[data-health-sync-now]');
  const line = document.querySelector('[data-health-sync-line]');
  const auto = document.querySelector('[data-health-auto]');
  const interval = document.querySelector('[data-health-interval]');
  box.hidden = false;
  document.querySelector('[data-health-edit-endpoint]').addEventListener('click', () => openShellSettings('endpoints'));
  const showStatus = () => {
    const st = healthSyncStatus();
    if (!st) return;
    // The endpoint's own switch (RetinueShell/3): off, the controls give way.
    const enabled = healthUploadEnabled(st);
    controls.hidden = !enabled;
    disabled.hidden = enabled;
    if (!enabled) return;
    line.textContent = describeHealthSync(st);
    auto.checked = !!st.auto_on_resume;
    if (document.activeElement !== interval && Number.isFinite(st.min_interval_minutes)) {
      interval.value = String(st.min_interval_minutes);
    }
  };
  now.addEventListener('click', () => {
    if (!healthSync()) { line.textContent = 'This phone shell cannot update health data.'; return; }
    now.disabled = true;
    line.textContent = 'Updating health data…';
  });
  auto.addEventListener('change', () => { setHealthAutoSync(auto.checked); showStatus(); });
  interval.addEventListener('change', () => {
    const minutes = Math.round(Number(interval.value));
    if (Number.isFinite(minutes) && minutes >= 1) setHealthSyncInterval(minutes);
    showStatus();
  });
  onHealthSync((d) => {
    if (d.state !== 'started') now.disabled = false;
    if (d.state === 'started' || d.state === 'skipped') {
      line.textContent = describeHealthSyncEvent(d);
    } else {
      showStatus();
      if (d.state === 'error' && !line.textContent) line.textContent = describeHealthSyncEvent(d);
    }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) showStatus(); });
  // The endpoint list changed (edited in the shell): so may the switch.
  onShellEndpoints(showStatus);
  showStatus();
}
