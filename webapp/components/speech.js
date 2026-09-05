// Read-aloud for the dashboard, on the browser's own speech synthesis.
//
// Why not Piper, which this stack already runs? Because that voice belongs to a
// *message* — a Signal voice note is an artefact the gateway produces, sends and
// stores. Reading a feed aloud is a property of the page you are looking at:
// nothing is produced, nothing is delivered, and the user expects to stop it
// mid-sentence and skip an item. The browser does that natively, with no server
// round-trip per headline, no audio to cache for offline, and with the voices
// the user has already installed and chosen on their own phone. It also keeps
// read-aloud working when the container is unreachable, which is exactly when a
// cached feed is worth listening to.
//
// Language handling is per item: whatever language tag an item carries is what
// we ask for, and the browser picks a matching voice if it has one. No language
// is special-cased here.
//
// The engine is not a media element, so the Reader below builds what a player
// needs on top of it: the text is cut into sentence-sized pieces spoken one
// after another, and the piece index is the position — pausing, seeking and
// skipping all work by cancelling the engine and speaking from another piece.
// That granularity (a sentence or two) is what the engines can actually do
// reliably; the workarounds for their known failure modes are marked below.

// Chrome stops speaking after roughly fifteen seconds of one utterance (with its
// network voices it dies silently, without an end event), and Android's engine
// rejects an utterance above a few thousand characters outright — so long text
// has to be handed over in pieces. About twelve seconds at a normal rate; a
// sentence or two. Splitting on sentence ends also gives natural places to
// stop when the user skips or pauses.
const MAX_CHUNK = 180;
// Speaking rate assumed before any piece has been timed, in characters per
// second (~150 words per minute); refined from the pieces actually completed.
const DEFAULT_RATE = 15;
const MIN_RATE = 4;
const MAX_RATE = 40;
// How often progress listeners hear an estimated position while a piece plays.
const TICK_MS = 250;
// Grace between cancel() and the next speak(): Chrome drops an utterance handed
// over in the same task as the cancel of the previous one.
const RESTART_DELAY_MS = 80;

export function speechAvailable() {
  return typeof window !== 'undefined' &&
    'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
}

function engine() { return window.speechSynthesis; }

// Cut text into pieces of at most MAX_CHUNK characters, on sentence ends where
// possible (a single overlong sentence is split on the nearest space).
export function chunk(text) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return [];
  const sentences = clean.match(/[^.!?…]+[.!?…]*\s*/g) || [clean];
  const out = [];
  let buf = '';
  for (const s of sentences) {
    if ((buf + s).length > MAX_CHUNK && buf) { out.push(buf.trim()); buf = ''; }
    if (s.length > MAX_CHUNK) {
      let rest = s;
      while (rest.length > MAX_CHUNK) {
        // No space to cut at (a long URL or hash): cut at the limit itself.
        const space = rest.lastIndexOf(' ', MAX_CHUNK);
        const cut = space > 0 ? space : MAX_CHUNK;
        out.push(rest.slice(0, cut).trim());
        rest = rest.slice(cut);
      }
      buf = rest;
    } else {
      buf += s;
    }
  }
  if (buf.trim()) out.push(buf.trim());
  return out;
}

// The installed voice for a language tag: an exact match first, then the same
// language in another region. Among equals a local voice wins — Chrome's
// network voices need connectivity and are the ones that die mid-utterance.
function pickVoice(lang) {
  if (!lang) return null;
  let voices = [];
  try { voices = engine().getVoices() || []; } catch (_) { return null; }
  const want = String(lang).toLowerCase();
  const base = want.split('-')[0];
  const local = (list) => list.find((v) => v.localService) || list[0] || null;
  const exact = voices.filter((v) => v.lang && v.lang.toLowerCase() === want);
  if (exact.length) return local(exact);
  const region = voices.filter((v) => v.lang && v.lang.toLowerCase().startsWith(base + '-'));
  if (region.length) return local(region);
  const bare = voices.filter((v) => v.lang && v.lang.toLowerCase() === base);
  return bare.length ? local(bare) : null;
}

// A player over a queue of {id, lang, text} items, each cut into pieces.
//
// States: 'idle' (nothing loaded), 'playing', 'paused'. The position is the
// character offset of the current piece within the whole queue (plus, while a
// piece plays, an estimate of how far into it the voice has got — from the
// engine's word-boundary events where it sends them, else from the measured
// speaking rate). Pausing and seeking restart at a piece boundary, so a resumed
// sentence is read again from its start — deliberate: that is the one behaviour
// every engine supports.
//
// `onstate(id)` fires when the item being read changes (null when done or
// stopped), so a view can highlight the row. `onprogress(ev)` fires on every
// transition and on a timer while playing, with `ev.event` one of load, start,
// piece (a piece was handed to the engine), tick, pause, seek, error, end,
// stop and the current `progress()`.
export class Reader {
  constructor(onstate) {
    this.onstate = onstate || (() => {});
    this.onprogress = () => {};
    this.pieces = [];
    this.pos = 0;
    this.total = 0;
    this.currentId = null;
    this.state = 'idle';
    // speak() was issued and the engine has not reported the start yet — the
    // gap in which a tap looks ignored, so the UI can say "starting".
    this.starting = false;
    this.error = null;
    // The live utterance is held here on purpose: Chrome garbage-collects an
    // unreferenced utterance mid-sentence and the speech stops with no event.
    this._utter = null;
    // Bumped on every restart/stop so late events and timers of a superseded
    // utterance are ignored.
    this._gen = 0;
    this._timer = null;
    this._ticker = null;
    this._rate = DEFAULT_RATE;
    this._startedAt = 0;
    this._boundary = 0;
    // When we last cancelled the engine: a speak() within the grace of that
    // is deferred even if the engine already claims to be idle.
    this._cancelledAt = 0;
    // Some browsers populate the voice list asynchronously; asking early is
    // what makes the first tap use the right voice.
    if (speechAvailable()) {
      try { engine().getVoices(); } catch (_) { /* ignore */ }
    }
  }

  get speaking() { return this.state === 'playing'; }
  get loaded() { return this.state !== 'idle'; }

  // Build the queue without speaking; the reader ends up paused at `opts.offset`
  // characters (or at `opts.fraction` of the whole). Returns the piece count.
  load(items, opts) {
    this._silence();
    this.pieces = [];
    let at = 0;
    for (const item of items || []) {
      for (const part of chunk(item.text)) {
        this.pieces.push({ id: item.id, lang: item.lang, text: part, start: at, end: at + part.length });
        at += part.length;
      }
    }
    this.total = at;
    this.error = null;
    const o = opts || {};
    const offset = typeof o.fraction === 'number' ? o.fraction * this.total : (o.offset || 0);
    this.pos = this.pieces.length ? this._pieceAt(offset) : 0;
    this.state = this.pieces.length ? 'paused' : 'idle';
    this._setCurrent(this.pieces.length ? this.pieces[this.pos].id : null);
    this._emit('load');
    return this.pieces.length;
  }

  // items: [{id, lang, text}] — one entry per news item; each is chunked.
  play(items) {
    if (!speechAvailable()) return false;
    if (!this.load(items)) { this.stop(); return false; }
    this.resume();
    return true;
  }

  resume() {
    if (this.state === 'idle') return;
    if (this.pos >= this.pieces.length) this.pos = 0;
    this.state = 'playing';
    this.error = null;
    this._start();
  }

  pause() {
    if (this.state !== 'playing') return;
    this.state = 'paused';
    this._silence();
    this._emit('pause');
  }

  toggle() {
    if (this.state === 'playing') this.pause(); else this.resume();
  }

  // Move to the piece containing that fraction of the whole text (0..1).
  seek(fraction) {
    this.seekChars(Math.max(0, Math.min(1, Number(fraction) || 0)) * this.total);
  }

  seekChars(offset) {
    if (this.state === 'idle') return;
    this._goto(this._pieceAt(offset), 'seek');
  }

  // One piece back — or to the start of the current piece when it has been
  // playing for a while, the way a previous-track button behaves.
  back() {
    if (this.state === 'idle') return;
    const intoPiece = this.state === 'playing' && this._startedAt &&
      (Date.now() - this._startedAt) > 3000;
    this._goto(intoPiece ? this.pos : Math.max(0, this.pos - 1), 'seek');
  }

  forward() {
    if (this.state === 'idle') return;
    if (this.pos + 1 >= this.pieces.length) { this._finish(); return; }
    this._goto(this.pos + 1, 'seek');
  }

  // Jump past everything belonging to the item currently being read.
  skip() {
    const id = this.currentId;
    if (id === null) return;
    let next = this.pos;
    while (next < this.pieces.length && this.pieces[next].id === id) next++;
    if (next >= this.pieces.length) { this._finish(); return; }
    this._goto(next, 'seek');
  }

  stop() {
    this._silence();
    this.pieces = [];
    this.pos = 0;
    this.total = 0;
    this.state = 'idle';
    this.error = null;
    this._setCurrent(null);
    this._emit('stop');
  }

  // After the page comes back to the foreground: a backgrounded engine may have
  // dropped the utterance without telling us. If we think we are playing and it
  // is silent, start the current piece again.
  resync() {
    if (this.state !== 'playing' || !speechAvailable()) return;
    if (!this._engineBusy()) this._start();
  }

  progress() {
    const piece = this.pieces[this.pos];
    let offset = piece ? piece.start : this.total;
    if (piece && this.state === 'playing' && this._startedAt) {
      const elapsed = (Date.now() - this._startedAt) / 1000;
      const est = Math.max(this._boundary, Math.floor(elapsed * this._rate));
      offset += Math.min(piece.text.length - 1, Math.max(0, est));
    }
    return {
      state: this.state,
      starting: this.starting,
      error: this.error,
      offset,
      total: this.total,
      fraction: this.total ? offset / this.total : 0,
      index: this.pos,
      count: this.pieces.length,
      // Seconds left at the measured rate — an estimate, for a label at most.
      remaining: Math.max(0, Math.round((this.total - offset) / this._rate)),
    };
  }

  _pieceAt(offset) {
    const o = Math.max(0, Number(offset) || 0);
    for (let i = 0; i < this.pieces.length; i++) {
      if (o < this.pieces[i].end) return i;
    }
    return Math.max(0, this.pieces.length - 1);
  }

  _goto(index, event) {
    this.pos = Math.max(0, Math.min(this.pieces.length - 1, index));
    this._setCurrent(this.pieces[this.pos].id);
    if (this.state === 'playing') this._start();
    else this._emit(event);
  }

  _setCurrent(id) {
    if (this.currentId === id) return;
    this.currentId = id;
    this.onstate(id);
  }

  _emit(event) {
    try { this.onprogress(Object.assign({ event }, this.progress())); } catch (_) { /* a listener's bug is not ours */ }
  }

  // Drop what the engine is doing for us, and forget it. Only an utterance of
  // ours is cancelled: the engine is shared with the page's other readers
  // (the news card has one too), whose speech is not ours to stop, and a
  // cancel() on an idle engine is what sets up the cancel/speak race. A
  // stuck engine is dealt with when playback starts (_start).
  _silence() {
    this._gen++;
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    if (this._ticker) clearInterval(this._ticker);
    this._ticker = null;
    const had = !!this._utter;
    this._utter = null;
    this.starting = false;
    this._startedAt = 0;
    this._boundary = 0;
    if (had && speechAvailable()) {
      this._cancelledAt = Date.now();
      try { engine().cancel(); } catch (_) { /* ignore */ }
    }
  }

  // Stop where we are without touching the engine: it has already dropped
  // (or refused) the utterance. With a reason it is an error the user can
  // retry from the same place; without one, a plain pause.
  _halt(kind) {
    this._gen++;
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    if (this._ticker) clearInterval(this._ticker);
    this._ticker = null;
    this._utter = null;
    this.starting = false;
    this._startedAt = 0;
    this._boundary = 0;
    this.state = 'paused';
    this.error = kind || null;
    this._emit(kind ? 'error' : 'pause');
  }

  _engineBusy() {
    try { return !!(engine().speaking || engine().pending); } catch (_) { return true; }
  }

  // (Re)start speaking at this.pos. When the engine is busy — with our own
  // previous piece, or with a zombie utterance from before a page hop — cancel
  // it and hand over the next piece a moment later (see RESTART_DELAY_MS); the
  // same grace applies right after any cancel, whatever the engine claims.
  // Otherwise speak right away: that keeps the first utterance inside the
  // user's tap, which iOS requires before it will speak at all.
  _start() {
    if (!speechAvailable()) { this._halt('unavailable'); return; }
    const gen = ++this._gen;
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    const busy = !!this._utter || this._engineBusy();
    const recent = (Date.now() - this._cancelledAt) < RESTART_DELAY_MS;
    this._utter = null;
    this.starting = true;
    this._startedAt = 0;
    this._boundary = 0;
    if (busy) {
      this._cancelledAt = Date.now();
      try { engine().cancel(); } catch (_) { /* ignore */ }
    }
    // Ticker and announcement first: a speak() that fails below halts the
    // reader (see _halt), and that must be the last word.
    if (!this._ticker) this._ticker = setInterval(() => this._emit('tick'), TICK_MS);
    this._emit('start');
    if (busy || recent) {
      this._timer = setTimeout(() => {
        this._timer = null;
        if (gen === this._gen) this._speakCurrent(gen);
      }, RESTART_DELAY_MS);
    } else {
      this._speakCurrent(gen);
    }
  }

  _speakCurrent(gen) {
    if (gen !== this._gen || this.state !== 'playing') return;
    if (this.pos >= this.pieces.length) { this._finish(); return; }
    const piece = this.pieces[this.pos];
    const utter = new SpeechSynthesisUtterance(piece.text);
    if (piece.lang) utter.lang = piece.lang;
    const voice = pickVoice(piece.lang);
    if (voice) utter.voice = voice;
    const live = () => gen === this._gen && this._utter === utter;
    utter.onstart = () => {
      if (!live()) return;
      // The position was announced at hand-over; the start only anchors the
      // in-piece estimate, which the next tick picks up.
      this.starting = false;
      this._startedAt = Date.now();
      this._boundary = 0;
    };
    utter.onboundary = (e) => {
      if (!live()) return;
      if (e && typeof e.charIndex === 'number' && e.charIndex > this._boundary) this._boundary = e.charIndex;
    };
    utter.onend = () => {
      if (!live()) return;
      this._learnRate(piece);
      this._utter = null;
      this.pos++;
      this._speakCurrent(gen);
    };
    utter.onerror = (e) => {
      if (!live()) return;
      const kind = (e && e.error) || 'error';
      // Our own cancel() reports here too, but never on a live utterance —
      // it is forgotten before the cancel. A live interruption is someone
      // else's: the page's other reader starting, or the platform taking
      // the audio away. That is a pause with the place kept, not a fault.
      if (kind === 'interrupted' || kind === 'canceled') { this._halt(null); return; }
      // Anything else — engine not ready, no permission, synthesis failure —
      // stops the queue where it is, so the user can retry from the same
      // place instead of the reader racing through every remaining piece.
      this._halt(kind);
    };
    this._utter = utter;
    // Until the engine reports the start, the position is the piece start —
    // the previous piece's timing must not leak into this one's estimate.
    this._startedAt = 0;
    this._boundary = 0;
    this._setCurrent(piece.id);
    try {
      // A cancel() can leave Chrome's engine paused; a paused engine queues
      // speak() calls forever without a sound.
      if (engine().paused) engine().resume();
      engine().speak(utter);
    } catch (_) {
      this._halt('error');
      return;
    }
    this._emit('piece');
  }

  // Average the observed speaking rate in, so the position estimate and the
  // time-left label track the voice the user actually has.
  _learnRate(piece) {
    if (!this._startedAt) return;
    const secs = (Date.now() - this._startedAt) / 1000;
    if (secs < 0.5) return;
    const measured = piece.text.length / secs;
    this._rate = Math.max(MIN_RATE, Math.min(MAX_RATE, 0.7 * this._rate + 0.3 * measured));
  }

  _finish() {
    // Report the end before clearing, so a listener can tell "read to the end"
    // from "stopped by the user" (the stop event follows either way).
    this.pos = this.pieces.length;
    this._silence();
    this.state = 'paused';
    this._emit('end');
    this.stop();
  }
}
