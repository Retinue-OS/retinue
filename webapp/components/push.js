// Push notification management for the Retinue dashboard PWA.
//
// The <retinue-push-optin> element lives on the settings page (settings.html),
// in `manage` mode: it always shows this device's push state — unsupported,
// blocked, off, or enabled — with the enable button, the one preference left
// (whether archived conversations notify this device too) and a disable
// button. Without the `manage` attribute it keeps its original
// dashboard-banner behaviour: hidden unless push is available and not yet
// enabled (no page uses that mode any more, but the contract stays).
//
// There is deliberately no per-device choice of *which* events notify: the
// attention model decides whether a push goes out (see push_notify.py,
// DEFAULT_PREFERENCES). #66's modes — new / new & stalled / all / off — sat
// behind it as a second, blinder filter and dropped what it had released. A
// device that should stay quiet is disabled, which also stops the browser
// subscription rather than only the fan-out.
//
// The preference lives on the server-side subscription record; the local copy
// in localStorage exists so the device can be re-registered with the same
// preference on every load (a subscription the browser rotated behind our
// back — or one lost when the server's store was reset — is restored
// silently). That upkeep must not depend on the element being on the page —
// the dashboard imports this module without one — so it also runs once at
// module level, below.
//
// Note on iOS: Safari only exposes the Push API to a PWA that has been added to
// the home screen. In an in-browser tab `PushManager` is absent; the settings
// page says so instead of failing silently.

const CONFIG_URL = '/push/config';
const SUBSCRIBE_URL = '/push/subscribe';
const UNSUBSCRIBE_URL = '/push/unsubscribe';
const STORAGE_KEY_ARCHIVED = 'retinue_notify_archived';
// Disable is this device's off switch, now that the 'off' notification mode is
// gone — and it has to outlive a reload: the upkeep at the bottom of this
// module re-registers any device whose permission is granted, which used to
// undo a disable on the next page load.
const STORAGE_KEY_OFF = 'retinue_push_disabled';
// Left behind on devices that chose one of #66's retired modes (see header).
// Only 'off' carried intent worth keeping: it becomes a real disable.
const LEGACY_MODE_KEY = 'retinue_notification_mode';

// What the settings page says once this device is notifying: where the
// decision is taken, now that it is taken in one place.
const ENABLED_NOTE = 'Notifications are on for this device. What rings is '
  + 'decided by your focus mode and how much an item matters — not per '
  + 'device; this device shows whatever gets through.';

function urlBase64ToUint8Array(base64) {
  const padded = (base64 + '='.repeat((4 - (base64.length % 4)) % 4))
    .replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function supported() {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

function storedArchived() {
  return localStorage.getItem(STORAGE_KEY_ARCHIVED) !== '0';
}

function storedOff() {
  return localStorage.getItem(STORAGE_KEY_OFF) === '1';
}

// One-time migration off #66's retired notification modes (see the header).
// A device left on 'off' meant it, and Disable is where that now lives; every
// other mode is dropped, and the device keeps notifying — on the attention
// model's say alone, which is the point of the change. Run before the element
// is defined, so the settings page reads the migrated state, not the old one.
function migrateLegacyMode() {
  const legacy = localStorage.getItem(LEGACY_MODE_KEY);
  if (legacy === null) return;
  localStorage.removeItem(LEGACY_MODE_KEY);
  if (legacy === 'off') localStorage.setItem(STORAGE_KEY_OFF, '1');
}

if (supported()) migrateLegacyMode();

async function serverKey() {
  const res = await fetch(CONFIG_URL, { cache: 'no-store' });
  if (!res.ok) return null;
  const cfg = await res.json();
  return cfg.enabled && cfg.publicKey ? cfg.publicKey : null;
}

// Register (or re-register) this device with the gateway, carrying the
// device's notification preference on the subscription record.
async function ensureSubscription(notifyArchived) {
  if (!supported() || Notification.permission !== 'granted') return false;
  const key = await serverKey();
  if (!key) return false;
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (sub) {
    // If the server's key changed, the old subscription can never be delivered
    // to; drop it and subscribe again rather than failing quietly forever.
    const existing = new Uint8Array(sub.options.applicationServerKey || new ArrayBuffer(0));
    const wanted = urlBase64ToUint8Array(key);
    const same = existing.length === wanted.length && existing.every((b, i) => b === wanted[i]);
    if (!same) {
      await sub.unsubscribe();
      sub = null;
    }
  }
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
  }
  const res = await fetch(SUBSCRIBE_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      subscription: sub,
      notify_archived: notifyArchived,
    }),
  });
  return res.ok;
}

// Stop deliveries to this device: the browser subscription first (the part
// that actually stops them), then the server record.
async function dropSubscription() {
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return;
  const endpoint = sub.endpoint;
  await sub.unsubscribe();
  await fetch(UNSUBSCRIBE_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ endpoint }),
  }).catch(() => {});
}

const CSS = `
  :host { display: none; }
  :host([visible]) { display: block; }
  :host([enabled]) { display: block; }
  /* Settings-page mode: the element always shows this device's state. */
  :host([manage]) { display: block; }
  .container {
    display: flex; align-items: center; gap: 8px; width: 100%;
    box-sizing: border-box;
    background: var(--card, #151922); color: var(--fg, #e7ebf2);
    border: 0; border-radius: var(--radius, 16px);
    padding: 8px 16px; font: inherit; font-size: .85rem;
    position: relative;
  }
  /* The author display:flex above would otherwise outrank the UA [hidden]
     rule, leaving the row visible when _init hides it. */
  .container[hidden] { display: none; }
  .container:hover { background: var(--card-2, #1c2230); }
  button {
    background: none; border: 0; padding: 0; font: inherit; color: inherit;
    cursor: pointer; display: flex; align-items: center; gap: 8px;
  }
  button:disabled { opacity: .6; cursor: default; }
  .ico { font-size: 1.1rem; }
  .muted { color: var(--muted, #8b93a3); font-size: .75rem; }
  .controls { display: none; align-items: center; gap: 8px; width: 100%; flex-wrap: wrap; }
  :host([enabled]) .controls { display: flex; }
  :host([enabled]) .btn-main { display: none; }
  .archived-opt { display: flex; align-items: center; gap: 4px; cursor: pointer; }
  .status { margin-left: auto; }
  /* State explanation (manage mode only): unsupported browser, blocked
     permission, or what enabling does. Hidden while empty. */
  .note { display: none; }
  :host([manage]) .note:not(:empty) {
    display: block; color: var(--muted, #8b93a3); font-size: .8rem; margin: 6px 4px 0;
  }
  .btn-off {
    display: none; font: inherit; font-size: .78rem; cursor: pointer;
    background: transparent; color: var(--muted, #8b93a3);
    border: 1px solid var(--line, rgba(231,235,242,.2)); border-radius: 8px;
    padding: 4px 9px;
  }
  :host([manage][enabled]) .btn-off { display: inline-block; }
  .btn-off:hover { color: var(--high, #ff6b6b); border-color: var(--high, #ff6b6b); }
`;

class RetinuePushOptIn extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>${CSS}</style>
      <div class="container">
        <button type="button" class="btn-main"><span class="ico">&#128276;</span><span class="lbl">Enable notifications</span></button>
        <div class="controls">
          <span class="muted">Notifications on</span>
          <label class="archived-opt muted" title="Also notify for messages in archived conversations">
            <input type="checkbox" class="archived-check" checked> Archived
          </label>
          <span class="status muted"></span>
          <button type="button" class="btn-off" title="Stop notifying this device">Disable</button>
        </div>
      </div>
      <div class="note"></div>
    `;
    this._btn = this.shadowRoot.querySelector('.btn-main');
    this._archivedCheck = this.shadowRoot.querySelector('.archived-check');
    this._status = this.shadowRoot.querySelector('.status');
    this._lblEl = this.shadowRoot.querySelector('.lbl');
    this._note = this.shadowRoot.querySelector('.note');
    this._btnOff = this.shadowRoot.querySelector('.btn-off');
    this._btn.addEventListener('click', () => this._enable());
    this._btnOff.addEventListener('click', () => this._disable());
    this._archivedCheck.addEventListener('change', () => this._prefsChanged());
  }

  connectedCallback() {
    this._init();
  }

  get manage() { return this.hasAttribute('manage'); }

  async _init() {
    const container = this.shadowRoot.querySelector('.container');
    if (!supported()) {
      // Only the settings page has anything useful to say about this state —
      // the note explains it, and the enable row would be a dead control.
      container.hidden = true;
      this._setNote('Notifications are not available in this browser. On '
        + 'iPhone/iPad, add the dashboard to the home screen first — Safari '
        + 'only offers push to an installed app.');
      return;
    }
    if (Notification.permission === 'denied') {
      container.hidden = true;
      this._setNote('Notifications are blocked for this site. Allow them in '
        + 'the browser’s site settings, then come back here.');
      return;
    }

    this._archivedCheck.checked = storedArchived();

    if (Notification.permission === 'granted' && !storedOff()) {
      this.setAttribute('enabled', '');
      this.setAttribute('visible', '');
      this._setNote(ENABLED_NOTE);
      ensureSubscription(this._prefs().notifyArchived).catch(() => {});
      return;
    }
    if (this.manage) {
      this._setNote('Notifications are off on this device. Enable them to be '
        + 'told when Ara needs a decision or answers while the dashboard is '
        + 'closed.');
    }
    if (Notification.permission === 'granted') {
      // Disabled here, but the browser permission is still granted: the
      // enable button only needs to clear the local off switch.
      this.setAttribute('visible', '');
      return;
    }
    try {
      if (await serverKey()) {
        this.setAttribute('visible', '');
      } else if (this.manage) {
        // The gateway reports push disabled (no pywebpush / no key): an
        // Enable button could only fail, so say why there is none.
        container.hidden = true;
        this._setNote('Push notifications are not configured on this server.');
      }
    } catch (_) { /* offline: stay hidden (the note still shows in manage mode) */ }
  }

  _setNote(text) {
    if (this._note) this._note.textContent = this.manage ? text : '';
  }

  // Drop this device's subscription and remember the choice, so the upkeep at
  // the bottom of this module does not re-register it on the next load.
  async _disable() {
    this._btnOff.disabled = true;
    try {
      localStorage.setItem(STORAGE_KEY_OFF, '1');
      await dropSubscription();
      this.removeAttribute('enabled');
      this.setAttribute('visible', '');
      this._setLabel('Enable notifications');
      this._btn.disabled = false;
      this._setNote('Notifications are off on this device.');
    } finally {
      this._btnOff.disabled = false;
    }
  }

  _prefs() {
    return { notifyArchived: this._archivedCheck.checked };
  }

  async _enable() {
    this._btn.disabled = true;
    this._setLabel('Enabling…');
    try {
      // requestPermission must run in the click handler's gesture context.
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        this._setLabel('Notifications blocked', true);
        return;
      }
      localStorage.removeItem(STORAGE_KEY_OFF);
      if (await ensureSubscription(this._prefs().notifyArchived)) {
        this.setAttribute('enabled', '');
        this.removeAttribute('visible');
        this._setNote(ENABLED_NOTE);
      } else {
        this._setLabel('Could not enable', true);
        this._btn.disabled = false;
      }
    } catch (err) {
      this._setLabel('Could not enable', true);
      this._btn.disabled = false;
    }
  }

  async _prefsChanged() {
    const { notifyArchived } = this._prefs();
    localStorage.setItem(STORAGE_KEY_ARCHIVED, notifyArchived ? '1' : '0');
    this._setStatus('Updating…');
    try {
      this._setStatus(await ensureSubscription(notifyArchived)
        ? 'Updated' : 'Update failed');
    } catch (err) {
      this._setStatus('Update failed');
    }
  }

  _setLabel(text, muted) {
    this._lblEl.textContent = text;
    this._lblEl.className = muted ? 'lbl muted' : 'lbl';
  }

  _setStatus(text) {
    this._status.textContent = text;
    clearTimeout(this._statusTimer);
    if (text === 'Updated') {
      this._statusTimer = setTimeout(() => { this._status.textContent = ''; }, 2000);
    }
  }
}

customElements.define('retinue-push-optin', RetinuePushOptIn);

// Subscription upkeep, independent of the element: any page that imports this
// module (the dashboard does) silently re-registers an already-granted device
// with its stored preference, so a browser-rotated or server-reset
// subscription heals on the pages people actually open — not only when they
// visit the settings page. A device the user disabled is left alone.
if (supported() && Notification.permission === 'granted') {
  if (storedOff()) {
    // Disabled here, but the browser permission is still granted: make sure
    // no subscription survived (a legacy 'off' mode, or a failed disable).
    dropSubscription().catch(() => {});
  } else {
    ensureSubscription(storedArchived()).catch(() => {});
  }
}
