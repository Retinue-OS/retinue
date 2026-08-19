// Shared voice-dictation UI for text inputs that can be spoken instead of
// typed: the recording row that replaces the input while the mic is live —
// abort ✕ on the left, a red dot + live waveform + elapsed time, and on the
// right the green review check ✓ and the send button ➤ — plus the status row
// shown while a dictation is transcribed (and sent), and the analyser-driven
// waveform renderer. Used by the conversation composer (conversations.js) and
// the project quick-command bar (project-page.js), so both bars stay visually
// and behaviourally identical by construction.
//
// The hosts own the MediaRecorder state machine and the semantics of the
// three buttons (what "review" and "send" mean is per-host); this module owns
// only the presentation.

export function canRecord() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

// The row that replaces the text input while the mic is live. Hosts wire
// [data-rec-abort] / [data-rec-check] / [data-rec-send] clicks themselves.
export function recordingRowHtml() {
  return `<div class="row rec-row">` +
    `<button type="button" class="rec-btn rec-abort" data-rec-abort ` +
    `title="Discard recording" aria-label="Discard recording">✕</button>` +
    `<div class="wave-wrap"><span class="rec-dot" aria-hidden="true"></span>` +
    `<canvas class="wave" data-wave aria-hidden="true"></canvas>` +
    `<span class="rec-time" data-rectime>0:00</span></div>` +
    `<button type="button" class="rec-btn rec-ok" data-rec-check ` +
    `title="Stop and transcribe for review" aria-label="Stop and transcribe for review">✓</button>` +
    `<button type="button" class="rec-btn rec-send" data-rec-send ` +
    `title="Stop, transcribe and send" aria-label="Stop, transcribe and send">➤</button>` +
    `</div>`;
}

// The row shown in place of the text input while a dictation job runs, so the
// input (and with it the phone keyboard) never has to reappear mid-flow.
// `label` is one of the hosts' own static strings — never user content.
export function statusRowHtml(label) {
  return `<div class="row rec-row"><div class="wave-wrap rec-status" role="status">` +
    `<span>${label}</span></div></div>`;
}

// Live waveform on the recording row's canvas, driven by a Web Audio analyser
// over the mic stream — with a simulated wave where that API is unavailable.
// The host's renders re-create the canvas, so each frame looks it up fresh in
// the host's shadow root rather than holding a reference.
export class Waveform {
  constructor(host) {
    this._host = host;      // the custom element (shadowRoot + computed styles)
    this._audioCtx = null;
    this._analyser = null;
    this._raf = null;
    this._phase = 0;
    this._startMs = 0;
  }

  start(stream) {
    this.stop();
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) {
      try {
        this._audioCtx = new AC();
        const srcNode = this._audioCtx.createMediaStreamSource(stream);
        const an = this._audioCtx.createAnalyser();
        an.fftSize = 128;
        an.smoothingTimeConstant = 0.7;
        srcNode.connect(an);
        this._analyser = an;
      } catch (_e) { this._analyser = null; }
    }
    this._phase = 0;
    this._startMs = Date.now();
    const tick = () => {
      this._draw();
      this._raf = requestAnimationFrame(tick);
    };
    tick();
  }

  stop() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = null;
    this._analyser = null;
    if (this._audioCtx) {
      try { this._audioCtx.close(); } catch (_e) { /* ignore */ }
      this._audioCtx = null;
    }
  }

  _draw() {
    const root = this._host.shadowRoot;
    if (!root) return;
    const timeEl = root.querySelector('[data-rectime]');
    if (timeEl) {
      const s = Math.max(0, Math.floor((Date.now() - this._startMs) / 1000));
      const txt = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
      if (timeEl.textContent !== txt) timeEl.textContent = txt;
    }
    const canvas = root.querySelector('[data-wave]');
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
    if (canvas.height !== Math.round(h * dpr)) canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    let accent = '#6ea8fe';
    try {
      const v = getComputedStyle(this._host).getPropertyValue('--accent').trim();
      if (v) accent = v;
    } catch (_e) { /* keep default */ }
    ctx.fillStyle = accent;
    const bars = Math.max(12, Math.min(48, Math.floor(w / 7)));
    this._phase += 0.22;
    let levels;
    if (this._analyser) {
      const data = new Uint8Array(this._analyser.frequencyBinCount);
      this._analyser.getByteFrequencyData(data);
      // Speech lives in the lower bins — spread those across the bars.
      const usable = Math.max(1, Math.floor(data.length * 0.75));
      levels = Array.from({ length: bars }, (_, i) =>
        data[Math.floor((i / bars) * usable)] / 255);
    } else {
      // No analyser: a plausible-looking simulated wave.
      levels = Array.from({ length: bars }, (_, i) => 0.35 +
        0.25 * Math.sin(i * 0.7 + this._phase) +
        0.18 * Math.sin(i * 1.3 - this._phase * 1.6) +
        0.08 * Math.random());
    }
    const bw = canvas.width / bars;
    const mid = canvas.height / 2;
    for (let i = 0; i < bars; i += 1) {
      const level = Math.min(1, Math.max(0.06, levels[i]));
      const bh = Math.max(2 * dpr, level * canvas.height);
      const x = i * bw + bw * 0.2;
      const bwid = bw * 0.6;
      const r = Math.min(bwid / 2, 2 * dpr);
      const y = mid - bh / 2;
      if (typeof ctx.roundRect === 'function') {
        ctx.beginPath();
        ctx.roundRect(x, y, bwid, bh, r);
        ctx.fill();
      } else {
        ctx.fillRect(x, y, bwid, bh);
      }
    }
  }
}

// Styles for the mic button, the recording row and the status row. Appended by
// each host to its own shadow-DOM stylesheet.
export const VOICE_CSS = `
  .mic { display: inline-flex; align-items: center; justify-content: center; height: 40px; width: 40px;
         flex: none; border-radius: 50%; background: var(--card-2, #1c2230); border: 0; cursor: pointer;
         color: var(--fg, #e7ebf2); font-size: 1.05rem; user-select: none;
         -webkit-tap-highlight-color: transparent; }
  .mic:hover { background: rgba(110, 168, 254, .2); }
  .mic[disabled] { opacity: .6; cursor: default; }
  @keyframes mic-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .55; } }
  .rec-row { align-items: center; }
  .wave-wrap { flex: 1; min-width: 0; height: 40px; display: flex; align-items: center;
               gap: 8px; background: var(--card-2, #1c2230); border-radius: 20px; padding: 0 12px; }
  .wave-wrap canvas.wave { flex: 1; min-width: 0; height: 26px; display: block; }
  .rec-dot { flex: none; width: 8px; height: 8px; border-radius: 50%;
             background: var(--high, #ff6b6b); animation: mic-pulse 1.2s ease-in-out infinite; }
  .rec-time { flex: none; color: var(--muted, #8b93a3); font-size: .78rem;
              font-variant-numeric: tabular-nums; }
  .rec-btn { flex: none; display: inline-flex; align-items: center; justify-content: center;
             width: 40px; height: 40px; border-radius: 50%; border: 0; cursor: pointer;
             font-size: 1.05rem; padding: 0; -webkit-tap-highlight-color: transparent; }
  .rec-btn:active { filter: brightness(1.12); }
  .rec-abort { background: var(--card-2, #1c2230); color: var(--high, #ff6b6b); }
  .rec-abort:hover { background: rgba(255, 107, 107, .18); }
  .rec-ok { background: var(--ok, #57c785); color: #0b0d12; }
  .rec-send { background: var(--accent, #6ea8fe); color: #0b0d12; padding-left: 2px; }
  .rec-status { justify-content: center; color: var(--muted, #8b93a3);
                font-size: .85rem; font-style: italic; }
`;
