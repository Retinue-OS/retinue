// Push notification opt-in for the Retinue dashboard PWA.
//
// Renders a single bell button that asks for notification permission and
// registers a Web Push subscription with the gateway. Once permission is
// granted it turns into a compact preference row: which events notify this
// device (the mode select) and whether archived conversations do too. The
// preferences live on the server-side subscription record; the local copy in
// localStorage only exists so `_init` can re-register the device with the same
// preferences on every load (a subscription the browser rotated behind our
// back — or one lost when the server's store was reset — is restored silently).
//
// Note on iOS: Safari only exposes the Push API to a PWA that has been added to
// the home screen. In an in-browser tab `PushManager` is absent and this element
// simply never appears; that is expected, not a failure.

const CONFIG_URL = '/push/config';
const SUBSCRIBE_URL = '/push/subscribe';
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
  .container {
    display: flex; align-items: center; gap: 8px; width: 100%;
    background: var(--card, #151922); color: var(--fg, #e7ebf2);
    border: 0; border-radius: var(--radius, 16px);
    padding: 8px 16px; font: inherit; font-size: .85rem;
    position: relative;
  }
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
        </div>
      </div>
    `;
    this._btn = this.shadowRoot.querySelector('.btn-main');
    this._select = this.shadowRoot.querySelector('.mode-select');
    this._archivedOpt = this.shadowRoot.querySelector('.archived-opt');
    this._archivedCheck = this.shadowRoot.querySelector('.archived-check');
    this._status = this.shadowRoot.querySelector('.status');
    this._lblEl = this.shadowRoot.querySelector('.lbl');
    this._btn.addEventListener('click', () => this._enable());
    this._select.addEventListener('change', () => this._prefsChanged());
    this._archivedCheck.addEventListener('change', () => this._prefsChanged());
  }

  connectedCallback() {
    this._init();
  }

  async _init() {
    if (!supported()) return;
    if (Notification.permission === 'denied') return;

    this._select.value = localStorage.getItem(STORAGE_KEY) || DEFAULT_MODE;
    this._archivedCheck.checked = localStorage.getItem(STORAGE_KEY_ARCHIVED) !== '0';
    this._syncControls();

    if (Notification.permission === 'granted') {
      this.setAttribute('enabled', '');
      this.setAttribute('visible', '');
      const { mode, notifyArchived } = this._prefs();
      ensureSubscription(mode, notifyArchived).catch(() => {});
      return;
    }
    try {
      if (await serverKey()) this.setAttribute('visible', '');
    } catch (_) { /* offline: stay hidden */ }
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
