// Push notification opt-in for the Retinue dashboard.
//
// Renders a single bell button that asks for notification permission and
// registers a Web Push subscription with the gateway. It hides itself whenever
// there is nothing to do — push unsupported, the server has no VAPID key, or a
// subscription already exists — so the dashboard stays uncluttered once set up.
//
// Note on iOS: Safari only exposes the Push API to a PWA that has been added to
// the home screen. On an in-browser tab `PushManager` is absent and this element
// simply never appears; that is expected, not a failure.

const CONFIG_URL = '/push/config';
const SUBSCRIBE_URL = '/push/subscribe';

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
async function ensureSubscription() {
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
    body: JSON.stringify(sub),
  });
  return res.ok;
}

const CSS = `
  :host { display: none; }
  :host([visible]) { display: block; }
  button {
    display: flex; align-items: center; gap: 8px; width: 100%;
    background: var(--card, #151922); color: var(--fg, #e7ebf2);
    border: 0; border-radius: var(--radius, 16px);
    padding: 12px 16px; font: inherit; font-size: .85rem; text-align: left;
    cursor: pointer;
  }
  button:disabled { opacity: .6; cursor: default; }
  .ico { font-size: 1.1rem; }
  .muted { color: var(--muted, #8b93a3); font-size: .75rem; }
`;

class RetinuePushOptIn extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML =
      `<style>${CSS}</style>` +
      `<button type="button"><span class="ico">&#128276;</span>` +
      `<span class="lbl">Enable notifications</span></button>`;
    this._btn = this.shadowRoot.querySelector('button');
    this._btn.addEventListener('click', () => this._enable());
  }

  connectedCallback() {
    this._init();
  }

  async _init() {
    if (!supported()) return;
    if (Notification.permission === 'denied') return;
    if (Notification.permission === 'granted') {
      // Nothing to ask: just make sure the gateway still knows this device.
      ensureSubscription().catch(() => {});
      return;
    }
    // Only offer the button if the server can actually send.
    try {
      if (await serverKey()) this.setAttribute('visible', '');
    } catch (_) { /* offline: stay hidden */ }
  }

  async _enable() {
    this._btn.disabled = true;
    this._label('Enabling…');
    try {
      // requestPermission must run in the click handler's gesture context.
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        this._label('Notifications blocked', true);
        return;
      }
      if (await ensureSubscription()) {
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

  _label(text, muted) {
    const el = this.shadowRoot.querySelector('.lbl');
    el.textContent = text;
    el.className = muted ? 'lbl muted' : 'lbl';
  }
}

customElements.define('retinue-push-optin', RetinuePushOptIn);
