// App quick-launch dock. Unlike the other cards this one has no data document:
// its buttons are configured by hand as child <a> elements in index.html. It
// simply lays them out as a compact bottom dock. tel:/sms:/mailto:/geo: are
// local OS actions that work offline; intent:// (Chromium only) can launch
// arbitrary installed apps. The anchors' inner icon/label spans stay in the
// light DOM, so their typography is styled from styles.css.
class RetinueAppLauncher extends HTMLElement {
  connectedCallback() {
    if (this.shadowRoot) return;
    const heading = this.getAttribute('heading') || '';
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        h2 { font-size: .82rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
             color: var(--muted, #8b93a3); margin: 0 0 8px; }
        .dock { display: grid; grid-auto-flow: column; grid-auto-columns: 1fr; gap: 8px; }
        ::slotted(a) {
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          gap: 2px; min-height: 56px; padding: 6px 4px; border-radius: 14px; text-decoration: none;
          background: var(--card, #151922); border: 1px solid var(--line, rgba(231, 235, 242, .08));
          color: var(--fg, #e7ebf2); font-size: .7rem; font-weight: 500;
          -webkit-tap-highlight-color: transparent;
        }
        ::slotted(a:active) { background: var(--accent, #6ea8fe); color: #0b0d12; }
        ::slotted(a:focus-visible) { outline: 2px solid var(--accent, #6ea8fe); outline-offset: 1px; }
      </style>
      ${heading ? `<h2>${heading.replace(/[<>&]/g, '')}</h2>` : ''}
      <div class="dock"><slot></slot></div>`;
  }
}
customElements.define('retinue-app-launcher', RetinueAppLauncher);
