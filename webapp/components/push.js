// Push notification management for the Retinue dashboard PWA.
//
// The <retinue-push-optin> element lives on the settings page (settings.html),
// in `manage` mode: it always shows this device's push state — unsupported,
// blocked, off, or enabled — with the enable button, the preference row (which
// events notify this device, whether archived conversations do too) and a
// disable button. Without the `manage` attribute it keeps its original
// dashboard-banner behaviour: hidden unless push is available and not yet
// enabled (no page uses that mode any more, but the contract stays).
//
// The preferences live on the server-side subscription record; the local copy
// in localStorage exists so the device can be re-registered with the same
// preferences on every load (a subscription the browser rotated behind our
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
const STORAGE_KEY = 'retinue_notification_mode';
const STORAGE_KEY_ARCHIVED = 'retinue_notify_archived';

// #66 names "new & stalled" as the default for devices with no stored choice.
const DEFAULT_MODE = 'new_and_stalled';

const MODES = [
  { id: 'all', label: 'All messages' },
  { id: 'new_only', label: 'New conversations only' },
  { id: 'new_and_stalled', label: 'New & stalled conversations' },
  { id: 'off', label: 'No notifications' },
];

// Modes that can fire on a message in an existing thread. Only these need the
// archived-conversations opt-out: a "new" event is a thread being opened,
// which is never archived, and "off" notifies on nothing.
const MESSAGE_MODES = ['all', 'new_and_stalled'];

function urlBase64ToUint8Array(base64) {
  const padded = (base64 + '='.repeat((4 - (base64.length % 4)) % 4))
    .replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function supported() {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

function storedMode() {
  return localStorage.getItem(STORAGE_KEY) || DEFAULT_MODE;
}

function storedArchived() {
  return localStorage.getItem(STORAGE_KEY_ARCHIVED) !== '0';
}

async function serverKey() {
  const res = await fetch(CONFIG_URL, { cache: 'no-store' });
  if (!res.ok) return null;
  const cfg = await res.json();
  return cfg.enabled && cfg.publicKey ? cfg.publicKey : null;
}

// Register (or re-register) this device with the gateway, carrying the
// device's notification preferences on the subscription record.
async function ensureSubscription(mode, notifyArchived) {
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
      notification_mode: mode,
      notify_archived: notifyArchived,
    }),
  });
  return res.ok;
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
  select {
    background: var(--bg, #0b0d12); color: var(--fg, #e7ebf2);
    border: 1px solid var(--line, rgba(231,235,242,.2));
    border-radius: 4px; font-size: 0.8rem; padding: 2px 4px;
  }
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
          <span class="muted">Mode:</span>
          <select class="mode-select">
            ${MODES.map(m => `<option value="${m.id}">${m.label}</option>`).join('')}
          </select>
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
    this._select = this.shadowRoot.querySelector('.mode-select');
    this._archivedOpt = this.shadowRoot.querySelector('.archived-opt');
    this._archivedCheck = this.shadowRoot.querySelector('.archived-check');
    this._status = this.shadowRoot.querySelector('.status');
    this._lblEl = this.shadowRoot.querySelector('.lbl');
    this._note = this.shadowRoot.querySelector('.note');
    this._btnOff = this.shadowRoot.querySelector('.btn-off');
    this._btn.addEventListener('click', () => this._enable());
    this._btnOff.addEventListener('click', () => this._disable());
    this._select.addEventListener('change', () => this._prefsChanged());
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

    this._select.value = storedMode();
    this._archivedCheck.checked = storedArchived();
    this._syncControls();

    if (Notification.permission === 'granted') {
      this.setAttribute('enabled', '');
      this.setAttribute('visible', '');
      const { mode, notifyArchived } = this._prefs();
      ensureSubscription(mode, notifyArchived).catch(() => {});
      return;
    }
    if (this.manage) {
      this._setNote('Notifications are off on this device. Enable them to be '
        + 'told when Ara needs a decision or answers while the dashboard is '
        + 'closed.');
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

  // Drop this device's subscription: browser-side first (the part that stops
  // deliveries), then the server record, then back to the opt-in state.
  async _disable() {
    this._btnOff.disabled = true;
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        const endpoint = sub.endpoint;
        await sub.unsubscribe();
        await fetch(UNSUBSCRIBE_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint }),
        }).catch(() => {});
      }
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
    return { mode: this._select.value, notifyArchived: this._archivedCheck.checked };
  }

  // The archived opt-out only applies to modes that notify on messages.
  _syncControls() {
    this._archivedOpt.style.display =
      MESSAGE_MODES.includes(this._select.value) ? '' : 'none';
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
      const { mode, notifyArchived } = this._prefs();
      if (await ensureSubscription(mode, notifyArchived)) {
        this.setAttribute('enabled', '');
        this.removeAttribute('visible');
        this._setNote('');
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
    const { mode, notifyArchived } = this._prefs();
    localStorage.setItem(STORAGE_KEY, mode);
    localStorage.setItem(STORAGE_KEY_ARCHIVED, notifyArchived ? '1' : '0');
    this._syncControls();
    this._setStatus('Updating…');
    try {
      this._setStatus(await ensureSubscription(mode, notifyArchived)
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
// with its stored preferences, so a browser-rotated or server-reset
// subscription heals on the pages people actually open — not only when they
// visit the settings page.
if (supported() && Notification.permission === 'granted') {
  ensureSubscription(storedMode(), storedArchived()).catch(() => {});
}
