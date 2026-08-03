// Push notification opt-in for the Retinue dashboard PWA.
//
// Renders a single bell button that asks for notification permission and
// registers a Web Push subscription with the gateway. It hides itself whenever
// there is nothing to do — push unsupported, the server has no VAPID key, or a
// subscription already exists — so the dashboard stays uncluttered once set up.
//
// Note on iOS: Safari only exposes the Push API to a PWA that has been added to
// the home screen. In an in-browser tab `PushManager` is absent and this element
// simply never appears; that is expected, not a failure.

const CONFIG_URL = '/push/config';
const SUBSCRIBE_URL = '/push/subscribe';
const STORAGE_KEY = 'retinue_notification_mode';

const MODES = [
  { id: 'all', label: 'All messages' },
  { id: 'new_only', label: 'New conversations only' },
  { id: 'new_and_stalled', label: 'New & stalled conversations' },
];

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

// Register (or re-register) this device with the gateway. Called on every load
// once permission is granted, so a subscription the browser rotated behind our
// back — or one lost when the server's store was reset — is restored silently.
async function ensureSubscription(mode = 'all') {
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
    body: JSON.stringify({ subscription: sub, notification_mode: mode }),
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
    cursor: pointer; position: relative;
  }
  .container:hover { background: var(--card-2, #1c2230); }
  button {
    background: none; border: 0; padding: 0; font: inherit; color: inherit;
    cursor: pointer; display: flex; align-items: center; gap: 8px;
  }
  button:disabled { opacity: .6; cursor: default; }
  .ico { font-size: 1.1rem; }
  .muted { color: var(--muted, #8b93a3); font-size: .75rem; }
  .controls { display: none; align-items: center; gap: 4px; }
  :host([enabled]) .controls { display: flex; }
  :host([enabled]) .btn-main { display: none; }
  select {
    background: var(--bg, #0b0d12); color: var(--fg, #e7ebf2);
    border: 1px solid var(--line, rgba(231,235,242,.2));
    border-radius: 4px; font-size: 0.8rem; padding: 2px 4px;
    margin-left: auto;
  }
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
        </div>
      </div>
    `;
    this._container = this.shadowRoot.querySelector('.container');
    this._btn = this.shadowRoot.querySelector('.btn-main');
    this._select = this.shadowRoot.querySelector('.mode-select');
    this._label = this.shadowRoot.querySelector('.lbl');
    this._btn.addEventListener('click', () => this._enable());
    this._select.addEventListener('change', (e) => this._changeMode(e.target.value));
  }

  connectedCallback() {
    this._init();
  }

  async _init() {
    if (!supported()) return;
    if (Notification.permission === 'denied') return;
    
    const mode = localStorage.getItem(STORAGE_KEY) || 'all';
    this._select.value = mode;

    if (Notification.permission === 'granted') {
      this.setAttribute('enabled', '');
      this.setAttribute('visible', '');
      ensureSubscription(mode).catch(() => {});
      return;
    }
    try {
      if (await serverKey()) this.setAttribute('visible', '');
    } catch (_) {}
  }

  async _enable() {
    this._btn.disabled = true;
    this._label('Enabling…');
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        this._label('Notifications blocked', true);
        return;
      }
      const mode = this._select.value;
      if (await ensureSubscription(mode)) {
        this.setAttribute('enabled', '');
        this.removeAttribute('visible');
      } else {
        this._label('Could not enable', true);
        this._btn.disabled = false;
      }
    } catch (err) {
      this._label('Could not enable', true);
      this._btn.disabled = false;
    }
  }

  async _changeMode(mode) {
    localStorage.setItem(STORAGE_KEY, mode);
    this._btn.disabled = true;
    this._label('Updating…');
    try {
      if (await ensureSubscription(mode)) {
        this._label('Updated');
        setTimeout(() => this._label(''), 2000);
      } else {
        this._label('Update failed', true);
        this._btn.disabled = false;
      }
    } catch (err) {
      this._label('Update failed', true);
      this._btn.disabled = false;
    }
  }

  _label(text, muted) {
    const el = this._label;
    el.textContent = text;
    el.className = muted ? 'lbl muted' : 'lbl';
  }
}

customElements.define('retinue-push-optin', RetinuePushOptIn);
