// Continuous conversation: the pure parts of the hands-free loop in a thread
// (components/conversation.js) — answers read aloud, then the mic opens by
// itself, a take ends when the speaker falls silent, and it is sent.
//
// What lives here is what can be decided without a page: when a take has
// ended (the voice-activity detector below, fed one level at a time with the
// time it was measured), which thread a wake opens, and the per-device
// settings and per-thread flags the loop keeps. The thread element owns the
// microphone, the recorder and the send path; this module only tells it what
// the levels mean. Kept free of DOM work at import time so the suite can run
// it under Node (tests/test_webapp_continuous.py); the two helpers that do
// touch the page (`chime`, `LevelMeter`) only do so when called.
//
// Nothing here knows a language: the detector listens for energy, not words,
// so it works the same whatever the user speaks.

// ── Per-device settings (Settings → Voice) ───────────────────────────────────
// How long a pause ends a take, and how long the open mic waits for speech
// before it closes again. Per device, like every other voice preference: a
// quiet desk and a car want different values, and neither belongs to the
// account.
export const SILENCE_KEY = 'retinue-voice-silence-ms';
export const NO_SPEECH_KEY = 'retinue-voice-nospeech-ms';
export const DEFAULT_SILENCE_MS = 1500;
export const DEFAULT_NO_SPEECH_MS = 8000;
// Bounds, so a typo cannot cut every sentence in half or leave the mic open
// for minutes.
const SILENCE_RANGE = [500, 5000];
const NO_SPEECH_RANGE = [2000, 30000];

function storage() {
  try { return typeof localStorage !== 'undefined' ? localStorage : null; } catch (_e) { return null; }
}

function clampMs(value, [lo, hi], fallback) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.round(Math.max(lo, Math.min(hi, n)));
}

export function voiceTimeouts() {
  const s = storage();
  let silence = null;
  let noSpeech = null;
  try {
    silence = s && s.getItem(SILENCE_KEY);
    noSpeech = s && s.getItem(NO_SPEECH_KEY);
  } catch (_e) { /* blocked storage: defaults */ }
  return {
    silenceMs: clampMs(silence, SILENCE_RANGE, DEFAULT_SILENCE_MS),
    noSpeechMs: clampMs(noSpeech, NO_SPEECH_RANGE, DEFAULT_NO_SPEECH_MS),
  };
}

// Store either timeout (milliseconds); returns what is in force afterwards,
// clamped, so the settings field can show the value actually used.
export function setVoiceTimeouts({ silenceMs, noSpeechMs } = {}) {
  const s = storage();
  try {
    if (s && silenceMs !== undefined) {
      s.setItem(SILENCE_KEY, String(clampMs(silenceMs, SILENCE_RANGE, DEFAULT_SILENCE_MS)));
    }
    if (s && noSpeechMs !== undefined) {
      s.setItem(NO_SPEECH_KEY, String(clampMs(noSpeechMs, NO_SPEECH_RANGE, DEFAULT_NO_SPEECH_MS)));
    }
  } catch (_e) { /* not kept: the defaults go on applying */ }
  return voiceTimeouts();
}

// ── Per-thread mode ──────────────────────────────────────────────────────────
// Which threads are in continuous mode on this device, and when each was last
// used in it: {<thread id>: {on, at}}. The flag is the toggle's state; `at`
// is what a wake goes by — the thread the user was just talking to is the
// one a wake phrase should carry on, not one switched on last week.
export const MODE_KEY = 'retinue-continuous';
// A wake goes back to the thread used in the mode within this window; after
// it, a wake starts a new thread (the shell contract, docs/dashboard.md).
export const WAKE_WINDOW_MS = 15 * 60 * 1000;
// Entries kept: the map is per device and grows by one per thread ever put in
// the mode, so the oldest are dropped.
const MODE_MAX = 50;

export function modeEntries() {
  const s = storage();
  try {
    const raw = s && s.getItem(MODE_KEY);
    const data = raw ? JSON.parse(raw) : {};
    return data && typeof data === 'object' && !Array.isArray(data) ? data : {};
  } catch (_e) { return {}; }
}

function saveEntries(entries) {
  const s = storage();
  if (!s) return;
  const ids = Object.keys(entries)
    .sort((a, b) => (Number(entries[b].at) || 0) - (Number(entries[a].at) || 0));
  const kept = {};
  ids.slice(0, MODE_MAX).forEach((id) => { kept[id] = entries[id]; });
  try { s.setItem(MODE_KEY, JSON.stringify(kept)); } catch (_e) { /* ignore */ }
}

export function isContinuous(id) {
  const e = id ? modeEntries()[id] : null;
  return !!(e && e.on);
}

// Switch a thread's mode. Switching it on counts as using it; switching it off
// keeps the last use, so a wake right after still finds the conversation the
// user was having (and puts it back in the mode).
export function setContinuous(id, on, now = Date.now()) {
  if (!id) return;
  const entries = modeEntries();
  const prev = entries[id] || {};
  entries[id] = { on: !!on, at: on ? now : (Number(prev.at) || 0) };
  saveEntries(entries);
}

// The thread was used in the mode just now (the mic opened, a take went out).
export function touchContinuous(id, now = Date.now()) {
  if (!id) return;
  const entries = modeEntries();
  const prev = entries[id];
  if (!prev || !prev.on) return;
  entries[id] = { on: true, at: now };
  saveEntries(entries);
}

// Which thread a wake opens: the one used in continuous mode most recently,
// if that was within the window — else null, and the wake starts a new
// thread. Pure over the stored entries, so the choice is testable.
export function pickWakeThread(entries, now, windowMs = WAKE_WINDOW_MS) {
  let best = null;
  let bestAt = -Infinity;
  for (const [id, e] of Object.entries(entries || {})) {
    const at = Number(e && e.at);
    if (!id || !Number.isFinite(at) || at <= 0) continue;
    // A use stamped in the future (a clock moved back) counts as now.
    const age = Math.max(0, now - at);
    if (age > windowMs) continue;
    if (at > bestAt) { best = id; bestAt = at; }
  }
  return best;
}

// ── Voice-activity detection ─────────────────────────────────────────────────
// A take ends on silence after speech, and the mic gives up on no speech at
// all. Both are decided over one number per frame: the signal's RMS level
// (0..1) from an AnalyserNode over the recorder's own stream. Speech is a
// level clearly above the room's noise floor. The floor is learned from the
// frames below the threshold (quickly downwards, slowly upwards) — which
// alone would never follow a room that gets louder for good: a fan or a TV
// switched on mid-take sits above the threshold from then on, and would read
// as one endless sentence. So a level that stays above the threshold without
// a single dip for stepMs is taken for the room, not for a voice — speech
// always has gaps between words — and the floor jumps to it. If that run was
// all the "speech" the take had, it was never speech: the take goes back to
// waiting for some (event `noise`), and its no-speech timeout still runs.
//
// States: 'waiting' (mic open, no speech yet), 'speaking', 'done'. feed()
// returns the event a frame caused, or null:
//   {type: 'speech'}            speech began (once per take, unless retracted)
//   {type: 'noise'}             what began as speech was the room getting
//                               louder: back to waiting, nothing was heard
//   {type: 'end', reason}       the take is over: 'silence' after speech, or
//                               'max' when it ran to the length cap (the
//                               caller should not trust such a take enough
//                               to send it unseen)
//   {type: 'timeout'}           no speech within noSpeechMs: close the mic
export const VAD_DEFAULTS = {
  silenceMs: DEFAULT_SILENCE_MS,
  noSpeechMs: DEFAULT_NO_SPEECH_MS,
  // Frames ignored at the start: the chime that opens the mic, and the click
  // of the recorder starting, must not count as the user speaking. They seed
  // the noise floor instead (their quietest level).
  warmupMs: 300,
  // Above the floor by this factor, and above this absolute level, is speech.
  ratio: 2.5,
  minLevel: 0.012,
  // A level must hold this long to count as speech: a door, a cough of the
  // phone's own speaker, a tap on the case is shorter.
  minSpeechMs: 120,
  // One take is at most this long, whatever the room does.
  maxMs: 120000,
  // Floor adaptation per frame: towards a quieter level quickly, towards a
  // louder (non-speech) level slowly.
  floorDown: 0.3,
  floorUp: 0.02,
  // A run above the threshold with no dip at all for this long is a louder
  // room, not a voice (see above).
  stepMs: 3000,
};

export class Vad {
  constructor(opts = {}) {
    this.opts = Object.assign({}, VAD_DEFAULTS, opts);
    this.reset();
  }

  reset() {
    this.state = 'waiting';
    this.startedAt = null;   // first frame's time
    this.floor = null;       // learned noise level
    this.heard = false;      // speech began in this take
    this._aboveSince = null; // the current run above the threshold began here
    this._lastVoice = null;  // last frame above the threshold, once speaking
    this._runMin = null;     // quietest level of the current run above it
    this._dipped = false;    // a frame fell below it since speech began
  }

  threshold() {
    const f = this.floor == null ? 0 : this.floor;
    return Math.max(this.opts.minLevel, f * this.opts.ratio);
  }

  feed(level, now) {
    if (this.state === 'done') return null;
    const o = this.opts;
    const lv = Math.max(0, Number(level) || 0);
    if (this.startedAt == null) this.startedAt = now;
    const elapsed = now - this.startedAt;
    if (elapsed < o.warmupMs) {
      this.floor = this.floor == null ? lv : Math.min(this.floor, lv);
      return null;
    }
    if (this.floor == null) this.floor = lv;
    let above = lv > this.threshold();
    if (above) {
      if (this._aboveSince == null) { this._aboveSince = now; this._runMin = lv; }
      this._runMin = Math.min(this._runMin, lv);
      if (now - this._aboveSince >= o.stepMs) {
        // The room got louder: its new level is the floor from here on.
        this.floor = this._runMin;
        this._aboveSince = null;
        this._runMin = null;
        above = lv > this.threshold();
        // Still above even the new floor: a fresh run starts here.
        if (above) { this._aboveSince = now; this._runMin = lv; }
        if (this.state === 'speaking' && !this._dipped) {
          this.state = 'waiting';
          this.heard = false;
          this._lastVoice = null;
          if (elapsed >= o.noSpeechMs) return this._done({ type: 'timeout' });
          return { type: 'noise' };
        }
      }
    } else {
      this._aboveSince = null;
      this._runMin = null;
    }

    if (this.state === 'waiting') {
      if (above) {
        if (now - this._aboveSince >= o.minSpeechMs) {
          this.state = 'speaking';
          this.heard = true;
          this._dipped = false;
          this._lastVoice = now;
          return { type: 'speech' };
        }
      } else {
        this._learn(lv);
      }
      if (elapsed >= o.noSpeechMs) return this._done({ type: 'timeout' });
      return null;
    }

    // Speaking: silence is counted from the last frame that was voice.
    if (above) {
      this._lastVoice = now;
    } else {
      this._dipped = true;
      this._learn(lv);
      if (now - this._lastVoice >= o.silenceMs) return this._done({ type: 'end', reason: 'silence' });
    }
    if (elapsed >= o.maxMs) return this._done({ type: 'end', reason: 'max' });
    return null;
  }

  _learn(lv) {
    const o = this.opts;
    const k = lv < this.floor ? o.floorDown : o.floorUp;
    this.floor += (lv - this.floor) * k;
  }

  _done(ev) {
    this.state = 'done';
    return ev;
  }
}

// RMS of one block of time-domain samples (floats in -1..1).
export function rms(samples) {
  if (!samples || !samples.length) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

// ── Page helpers (touch Web Audio only when called) ──────────────────────────

// One AudioContext for the chime and the level meter, made — and resumed —
// inside a user gesture where there is one (primeAudio, from the loop's
// toggle or the mic). A context first made later, when the mic opens by
// itself after an answer, has no gesture behind it, and an autoplay policy
// may keep it suspended: the chime would be silent and the meter deaf.
let sharedCtx = null;

export function audioContext() {
  const AC = typeof window !== 'undefined' && (window.AudioContext || window.webkitAudioContext);
  if (!AC) return null;
  if (sharedCtx && sharedCtx.state !== 'closed') return sharedCtx;
  try { sharedCtx = new AC(); } catch (_e) { sharedCtx = null; }
  return sharedCtx;
}

// Call from a tap: makes the shared context and lets it run.
export function primeAudio() {
  const ctx = audioContext();
  try { if (ctx && ctx.state === 'suspended' && ctx.resume) ctx.resume().catch(() => {}); } catch (_e) { /* ignore */ }
  return ctx;
}

// The short rising two-note chime that says "the mic is open now". Resolves
// when it has played (or at once where Web Audio is missing), so the mic's
// detector starts listening after it rather than hearing it.
export function chime() {
  const ctx = audioContext();
  if (!ctx) return Promise.resolve();
  try {
    if (ctx.state === 'suspended' && ctx.resume) ctx.resume().catch(() => {});
    const t0 = ctx.currentTime + 0.02;
    [[660, 0], [880, 0.09]].forEach(([freq, at]) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t0 + at);
      gain.gain.exponentialRampToValueAtTime(0.18, t0 + at + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.12);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t0 + at);
      osc.stop(t0 + at + 0.13);
    });
  } catch (_e) { /* no chime is no reason to keep the mic shut */ }
  return new Promise((resolve) => setTimeout(resolve, 260));
}

// Feeds a Vad from a live stream: an AnalyserNode over it, sampled on a
// timer (not animation frames — those stop when the screen is covered, and a
// take must still end). `onEvent` gets each event feed() returns. start()
// resolves to false where Web Audio cannot hear the stream (missing, or a
// context the browser keeps suspended): the caller then falls back to plain
// timers (conversation.js, _contUnheard) and the take is ended by hand.
export class LevelMeter {
  constructor(vad, onEvent, intervalMs = 50) {
    this.vad = vad;
    this.onEvent = onEvent;
    this.intervalMs = intervalMs;
    this._src = null;
    this._timer = null;
    this._run = 0;
  }

  async start(stream) {
    this.stop();
    const run = this._run;
    const ctx = audioContext();
    if (!ctx) return false;
    try {
      if (ctx.state === 'suspended' && ctx.resume) {
        // An autoplay policy may hold the context until a gesture; a hold
        // means silence, which would read as "nobody spoke".
        await Promise.race([ctx.resume(), new Promise((r) => setTimeout(r, 400))]);
      }
      if (this._run !== run) return false; // stopped meanwhile
      if (ctx.state !== 'running') return false;
      const src = ctx.createMediaStreamSource(stream);
      const an = ctx.createAnalyser();
      an.fftSize = 1024;
      src.connect(an);
      this._src = src;
      const buf = new Float32Array(an.fftSize);
      this.vad.reset();
      this._timer = setInterval(() => {
        if (typeof an.getFloatTimeDomainData === 'function') {
          an.getFloatTimeDomainData(buf);
        } else {
          const bytes = new Uint8Array(an.fftSize);
          an.getByteTimeDomainData(bytes);
          for (let i = 0; i < bytes.length; i++) buf[i] = (bytes[i] - 128) / 128;
        }
        const ev = this.vad.feed(rms(buf), Date.now());
        if (ev) {
          if (ev.type === 'end' || ev.type === 'timeout') this.stop();
          try { this.onEvent(ev); } catch (_e) { /* the listener's own bug */ }
        }
      }, this.intervalMs);
      return true;
    } catch (_e) {
      this.stop();
      return false;
    }
  }

  // The shared context stays: the next take (and its chime) reuse it.
  stop() {
    this._run += 1;
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
    if (this._src) {
      try { this._src.disconnect(); } catch (_e) { /* ignore */ }
      this._src = null;
    }
  }
}
