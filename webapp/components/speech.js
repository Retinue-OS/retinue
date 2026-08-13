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

// Chrome stops speaking after roughly fifteen seconds of one utterance, so long
// text has to be handed over in pieces. Splitting on sentence ends also gives
// the reader natural places to stop when the user taps skip.
const MAX_CHUNK = 220;

export function speechAvailable() {
  return typeof window !== 'undefined' &&
    'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
}

function chunk(text) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return [];
  const sentences = clean.match(/[^.!?…]+[.!?…]*\s*/g) || [clean];
  const out = [];
  let buf = '';
  for (const s of sentences) {
    if ((buf + s).length > MAX_CHUNK && buf) { out.push(buf.trim()); buf = ''; }
    // A single sentence longer than the limit is split on the nearest space.
    if (s.length > MAX_CHUNK) {
      let rest = s;
      while (rest.length > MAX_CHUNK) {
        const cut = rest.lastIndexOf(' ', MAX_CHUNK) || MAX_CHUNK;
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

function pickVoice(lang) {
  if (!lang) return null;
  let voices = [];
  try { voices = speechSynthesis.getVoices() || []; } catch (_) { return null; }
  const want = String(lang).toLowerCase();
  const base = want.split('-')[0];
  return voices.find((v) => v.lang && v.lang.toLowerCase() === want) ||
    voices.find((v) => v.lang && v.lang.toLowerCase().startsWith(base + '-')) ||
    voices.find((v) => v.lang && v.lang.toLowerCase() === base) || null;
}

// A queue of {id, text, lang} pieces read one after another. `onstate` is called
// with the id currently being read (or null) so a view can highlight the row.
export class Reader {
  constructor(onstate) {
    this.onstate = onstate || (() => {});
    this.pieces = [];
    this.pos = 0;
    this.currentId = null;
    // Some browsers populate the voice list asynchronously; re-reading it on
    // this event is what makes the first tap use the right voice.
    if (speechAvailable()) {
      try { speechSynthesis.getVoices(); } catch (_) { /* ignore */ }
    }
  }

  get speaking() { return this.currentId !== null; }

  // items: [{id, lang, text}] — one entry per news item; each is chunked.
  play(items) {
    if (!speechAvailable()) return false;
    this.stop();
    this.pieces = [];
    for (const item of items || []) {
      for (const part of chunk(item.text)) {
        this.pieces.push({ id: item.id, lang: item.lang, text: part });
      }
    }
    this.pos = 0;
    this._speakCurrent();
    return this.pieces.length > 0;
  }

  // Jump past everything belonging to the item currently being read.
  skip() {
    const id = this.currentId;
    if (id === null) return;
    while (this.pos < this.pieces.length && this.pieces[this.pos].id === id) this.pos++;
    try { speechSynthesis.cancel(); } catch (_) { /* ignore */ }
    this._speakCurrent();
  }

  stop() {
    this.pieces = [];
    this.pos = 0;
    try { speechSynthesis.cancel(); } catch (_) { /* ignore */ }
    this._setCurrent(null);
  }

  _setCurrent(id) {
    if (this.currentId === id) return;
    this.currentId = id;
    this.onstate(id);
  }

  _speakCurrent() {
    if (this.pos >= this.pieces.length) { this.stop(); return; }
    const piece = this.pieces[this.pos];
    const utter = new SpeechSynthesisUtterance(piece.text);
    if (piece.lang) utter.lang = piece.lang;
    const voice = pickVoice(piece.lang);
    if (voice) utter.voice = voice;
    utter.onend = () => {
      // A cancel() also fires onend; the guard keeps a stopped reader stopped.
      if (this.pieces[this.pos] !== piece) return;
      this.pos++;
      this._speakCurrent();
    };
    utter.onerror = () => {
      if (this.pieces[this.pos] !== piece) return;
      this.pos++;
      this._speakCurrent();
    };
    this._setCurrent(piece.id);
    try { speechSynthesis.speak(utter); } catch (_) { this.stop(); }
  }
}
